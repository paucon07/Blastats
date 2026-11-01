import json
import os
import time
from datetime import datetime, timezone

import grpc
import requests

import db

# reads the youtube live chat and passes camera commands to the spectator mod
# the latest command goes in camera_command txt in the data folder which the mod checks every second
# needs a youtube data api key in the YOUTUBE_API_KEY environment variable

LOG_FILE = os.path.join(db.DATA_DIR, "chat.log")
STREAM_FILE = os.path.join(db.DATA_DIR, "stream_info.json")
COMMAND_FILE = os.path.join(db.DATA_DIR, "camera_command.txt")
CHAT_HOST = "youtube.googleapis.com:443"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
REQUEST_TIMEOUT = 15  # seconds before a request gives up

COOLDOWN = 15  # seconds before chat can change the camera again
# every chat connection costs 5 of the 10000 daily quota units
# one a minute at most keeps a 24/7 stream under it
MIN_CONNECT_GAP = 60
OFFLINE_WAIT = 300  # seconds between checks while the stream isnt live
QUOTA_WAIT = 3600  # seconds to back off once the daily quota runs out

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
    r = requests.get(VIDEOS_URL, params={"part": "liveStreamingDetails", "id": video, "key": key},
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    items = r.json().get("items") or []
    return (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") if items else None

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

# keep a chat stream open and reconnect from where it left off when it closes
def main():
    global current, started
    os.makedirs(db.DATA_DIR, exist_ok=True)
    key = api_key()
    started = datetime.now(timezone.utc)
    # carry on from the last command so repeating it after a restart is ignored too
    if os.path.exists(COMMAND_FILE):
        with open(COMMAND_FILE, "r", encoding="utf-8") as f:
            current = f.read().strip() or None
    log("chat camera starting")

    metadata = (("x-goog-api-key", key),)
    chat_id, token, last_connect = None, None, 0.0
    offline_logged = False  # so a long offline spell is logged once
    with grpc.secure_channel(CHAT_HOST, grpc.ssl_channel_credentials()) as channel:
        stub = services.V3DataLiveChatMessageServiceStub(channel)
        while True:
            # one connection a minute at most whatever happened to the last one
            time.sleep(max(0.0, last_connect + MIN_CONNECT_GAP - time.time()))
            if chat_id is None:
                try:
                    chat_id = live_chat_id(key)
                except requests.RequestException as e:
                    log(f"video lookup error: {e}")
                if chat_id is None:
                    if not offline_logged:
                        log(f"stream isnt live, checking every {OFFLINE_WAIT // 60} minutes")
                        offline_logged = True
                    time.sleep(OFFLINE_WAIT)
                    continue
                token, offline_logged = None, False
                log("reading live chat")

            last_connect = time.time()
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
                log(f"chat stream closed after {time.time() - last_connect:.0f}s")
            except grpc.RpcError as e:
                log(f"chat stream error {e.code().name}: {e.details()}")
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    raise SystemExit("youtube rejected the api key")
                if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    time.sleep(QUOTA_WAIT)
                # the chat id goes stale when the stream restarts so look it up again
                if e.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT,
                                grpc.StatusCode.FAILED_PRECONDITION):
                    chat_id = None

# run alongside the poller while the stream is live
if __name__ == "__main__":
    main()
