import json
import os
import time
from datetime import datetime, timedelta, timezone

import grpc
import requests

import db

# reads the youtube live chat and passes camera commands to the spectator mod
# the latest command goes in camera_command txt in the data folder which the mod checks every second
# chat is only read while a duel is on since the camera has nothing to show between matches
# needs a youtube data api key in the YOUTUBE_API_KEY environment variable

LOG_FILE = os.path.join(db.DATA_DIR, "chat.log")
STREAM_FILE = os.path.join(db.DATA_DIR, "stream_info.json")
COMMAND_FILE = os.path.join(db.DATA_DIR, "camera_command.txt")
# the mod rewrites this every 5 seconds while both players are in the arena
DUEL_FILE = os.path.join(db.DATA_DIR, "duel_live.txt")
DUEL_FRESH = 15  # seconds since the last rewrite that still counts as a duel
CHAT_HOST = "youtube.googleapis.com:443"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
REQUEST_TIMEOUT = 15  # seconds before a request gives up

COOLDOWN = 15  # seconds before chat can change the camera again
OFFLINE_WAIT = 300  # seconds between checks while the stream isnt live
QUOTA_WAIT = 3600  # seconds to back off if youtube says the quota ran out anyway

# quota
# youtube allows 10000 units a day and a chat connection costs 5 and a video lookup 1
# it closes chat connections after about 10 seconds so they cant be held open all day
# duels are about a tenth of the day which comes to around 4000 units
# the units used are also paced across the day in case a busy day runs over
QUOTA_FILE = os.path.join(db.DATA_DIR, "chat_quota.json")
DAILY_BUDGET = 9500  # leaves a little spare under the real limit
# how far ahead of an even pace it can spend before reconnects slow down
QUOTA_CUSHION = 3000
CONNECT_COST = 5
MIN_CONNECT_GAP = 2  # seconds so a connection closing straight away cant burn through quota
LOOKUP_COST = 1
# quota resets at midnight pacific and using pst all year is close enough for pacing
QUOTA_TZ = timezone(timedelta(hours=-8))

# chat command to the view the mod understands
COMMANDS = {"/auto": "auto", "/autocamera": "auto",
            "/thirdperson": "thirdperson", "/firstperson": "firstperson"}
COLOURS = ("pink", "blue")

# message classes and the client are built from the proto when the script starts
protos, services = grpc.protos_and_services("youtube_chat.proto")
TEXT_MESSAGE = protos.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT
CHAT_ENDED = protos.LiveChatMessageSnippet.TypeWrapper.CHAT_ENDED_EVENT

# last command sent and when so repeats and the cooldown can be checked
current = None
last_change = 0.0
# messages from before the script started are skipped so a restart doesnt replay old chat
started = None
# units used on the current quota day
quota = {"day": None, "used": 0}

# helpers
# print a timestamped line and append it to the log file
def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# api key read from an environment variable like the poller login
def api_key():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise SystemExit("set YOUTUBE_API_KEY to a youtube data api key")
    return key

# chat id of the video in stream info or none when it isnt live
# the video id is the same one the site uses for match timestamps
def live_chat_id(key):
    try:
        with open(STREAM_FILE, "r", encoding="utf-8") as f:
            video = json.load(f).get("videoId")
    except (OSError, json.JSONDecodeError):
        video = None
    if not video:
        log("no videoId in stream_info.json")
        return None
    spend(LOOKUP_COST)
    r = requests.get(VIDEOS_URL, params={"part": "liveStreamingDetails", "id": video, "key": key},
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    items = r.json().get("items") or []
    return (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") if items else None

# true while the mod has marked a duel in the last few seconds
def duel_on():
    try:
        return time.time() - os.path.getmtime(DUEL_FILE) < DUEL_FRESH
    except OSError:
        return False

# quota
# start of the current quota day
def quota_day_start():
    now = datetime.now(QUOTA_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)

# units used today, kept in a file so a restart doesnt forget them
def load_quota():
    global quota
    if os.path.exists(QUOTA_FILE):
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            try: quota = json.load(f)
            except json.JSONDecodeError: pass

# count units against today and start again on a new day
def spend(units):
    day = quota_day_start().date().isoformat()
    if quota.get("day") != day:
        quota.update(day=day, used=0)
    quota["used"] += units
    with open(QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(quota, f)

# seconds to wait before a chat connection so the day stays under the budget
# spending can run ahead of an even pace by the cushion then slows to the even pace
def quota_wait():
    start = quota_day_start()
    used = quota["used"] if quota.get("day") == start.date().isoformat() else 0
    elapsed = (datetime.now(QUOTA_TZ) - start).total_seconds()
    rate = DAILY_BUDGET / 86400  # units per second at an even pace
    allowed = min(DAILY_BUDGET, rate * elapsed + QUOTA_CUSHION)
    over = used + CONNECT_COST - allowed
    if used + CONNECT_COST > DAILY_BUDGET:
        return 86400 - elapsed  # out for today so wait for the reset
    return max(0.0, over / rate)

# commands
# view for a chat message or none when it isnt a command
# a colour pins the camera to that player and without one it follows round winners
def parse_command(text):
    words = (text or "").lower().split()
    if not words or words[0] not in COMMANDS:
        return None
    view = COMMANDS[words[0]]
    if view != "auto" and len(words) > 1 and words[1] in COLOURS:
        return f"{view} {words[1]}"
    return view

# swap the whole file in at once so the mod never reads half of it
def write_command(command):
    tmp = COMMAND_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(command)
    # windows refuses the swap for a moment while the mod is reading the old file
    for attempt in range(5):
        try:
            os.replace(tmp, COMMAND_FILE)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)

# act on one chat message
# returns false once the chat has ended
def handle(message):
    global current, last_change
    s = message.snippet
    if s.type == CHAT_ENDED:
        return False
    if s.type != TEXT_MESSAGE:
        return True
    sent = db.parse_ts(s.published_at)
    if sent is None or sent < started:
        return True
    command = parse_command(s.text_message_details.message_text)
    # a repeat of the current view doesnt use up the cooldown
    if command is None or command == current or time.time() - last_change < COOLDOWN:
        return True
    write_command(command)
    current, last_change = command, time.time()
    log(f"{message.author_details.display_name}: {command}")
    return True

# read the chat during duels and reconnect from where it left off each time youtube closes it
# commands sent between duels arrive when the next one starts
def main():
    global current, started
    os.makedirs(db.DATA_DIR, exist_ok=True)
    key = api_key()
    started = datetime.now(timezone.utc)
    load_quota()
    # carry on from the last command so repeating it after a restart is ignored too
    if os.path.exists(COMMAND_FILE):
        with open(COMMAND_FILE, "r", encoding="utf-8") as f:
            current = f.read().strip() or None
    log("chat camera starting")

    metadata = (("x-goog-api-key", key),)
    chat_id, token, last_connect = None, None, 0.0
    # so long offline spells and slowing down are only logged once
    state = None
    with grpc.secure_channel(CHAT_HOST, grpc.ssl_channel_credentials()) as channel:
        stub = services.V3DataLiveChatMessageServiceStub(channel)
        while True:
            if chat_id is None:
                try:
                    chat_id = live_chat_id(key)
                except requests.RequestException as e:
                    log(f"video lookup error: {e}")
                if chat_id is None:
                    if state != "offline":
                        log(f"stream isnt live, checking every {OFFLINE_WAIT // 60} minutes")
                        state = "offline"
                    time.sleep(OFFLINE_WAIT)
                    continue
                token, state = None, "reading"
                log("found the live chat")

            if not duel_on():
                time.sleep(1)
                continue

            wait = quota_wait()
            if wait > 0:
                if state != "slow":
                    log(f"spent {quota['used']} quota units today so reconnects are slowing down")
                    state = "slow"
                time.sleep(min(wait, 60))
                continue
            state = "reading"

            time.sleep(max(0.0, last_connect + MIN_CONNECT_GAP - time.time()))
            last_connect = time.time()
            spend(CONNECT_COST)
            request = protos.LiveChatMessageListRequest(live_chat_id=chat_id, part=["snippet", "authorDetails"],
                                                        page_token=token)
            try:
                for response in stub.StreamList(request, metadata=metadata):
                    # the token lets the next connection pick up after the last message seen
                    if response.next_page_token:
                        token = response.next_page_token
                    ended = False
                    for message in response.items:
                        if not handle(message):
                            ended = True
                    if ended or response.offline_at:
                        log("live chat ended")
                        chat_id = None
                        break
            except grpc.RpcError as e:
                log(f"chat stream error {e.code().name}: {e.details()}")
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    raise SystemExit("youtube rejected the api key")
                if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    time.sleep(QUOTA_WAIT)
                # the chat id goes stale when the stream restarts so look it up again
                if e.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT,
                                grpc.StatusCode.FAILED_PRECONDITION):
                    chat_id, token = None, None
                # dont hammer youtube while something is wrong
                time.sleep(5)

# run alongside the poller while the stream is live
if __name__ == "__main__":
    main()
