import requests
import json
import os
import time
from datetime import datetime, timezone

import rectify  # local match rectifier that writes data corrections json

# file paths
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LEADERBOARD_FILE = os.path.join(DATA_DIR, "leaderboard.json")
PLAYERS_FILE = os.path.join(DATA_DIR, "players.json")
LOG_FILE = os.path.join(DATA_DIR, "poller.log")
SEASON_FILE = os.path.join(DATA_DIR, "season.json")
ALL_MATCHES_FILE = os.path.join(DATA_DIR, "all_matches.json")

# playfab title and poll intervals in seconds
PLAYFAB_TITLE_ID = "B0549"
PLAYFAB_BASE = f"https://{PLAYFAB_TITLE_ID}.playfabapi.com"
POLL_INTERVAL_LEADERBOARD = 300
POLL_INTERVAL_PLAYER_STATS = 900

# cap on single player stat lookups per stats cycle
BACKFILL_MAX_CALLS = 100
# wait 6 hours before refetching an off ladder stat
BACKFILL_REFRESH_INTERVAL = 6 * 3600

# cap on account age lookups per stats cycle
ACCOUNT_AGE_MAX_CALLS = 75
# retry failed account lookups once a day
ACCOUNT_RECHECK_INTERVAL = 24 * 3600

# stat ladders to track
STAT_NAMES = [
    "ELO_RATING",
    "LEAGUE_POINTS_TOTAL",
    "ROUNDS_WON",
    "CALCULATED_MATCHMAKING_VALUE",
]

# headers copied from the unity game client
BASE_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "User-Agent": "UnityPlayer/6000.0.40f1 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
    "X-PlayFabSDK": "UnitySDK-2.153.221024",
    "X-ReportErrorAsSuccess": "true",
    "X-Unity-Version": "6000.0.40f1",
}

SDK_VERSION = "UnitySDK-2.153.221024"
REQUEST_TIMEOUT = 15  # seconds before a request gives up

# http sessions
# playfab session with base headers and sdk param on every request
playfab = requests.Session()
playfab.headers.update(BASE_HEADERS)
playfab.params = {"sdk": SDK_VERSION}

# match server session with no sdk param
match_repo = requests.Session()
match_repo.headers.update(BASE_HEADERS)

# post to a playfab client endpoint and return the json reply
def playfab_post(endpoint, body):
    return playfab.post(f"{PLAYFAB_BASE}/Client/{endpoint}",
                        json=body, timeout=REQUEST_TIMEOUT).json()

# helpers
# print a timestamped line and append it to the log file
def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# read a json file or return default if missing or broken
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return default
    return default

# write to a temp file then swap it in
# readers never see a half written file
def save_json(path, data):
    # temp name per process so nothing clashes
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # windows blocks the swap while a reader has the file open so retry briefly
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)

# auth
session_ticket = None
entity_token = None
auth_expiry = 0

# log in to playfab and store the session ticket
def authenticate():
    global session_ticket, entity_token, auth_expiry
    # use any playfab id
    custom_id = os.environ.get("BLASTATS_CUSTOM_ID")
    if not custom_id:
        log("BLASTATS_CUSTOM_ID not set, cant log in")
        return False
    log(f"Authenticating (CustomId: {custom_id[:8]}...)")
    # drop the old ticket before logging in again
    playfab.headers.pop("X-Authorization", None)
    # turn off all the extra login info
    r = playfab_post("LoginWithCustomID", {
        "CreateAccount": True, "CustomId": custom_id, "TitleId": PLAYFAB_TITLE_ID,
        "InfoRequestParameters": {k: False for k in [
            "GetCharacterInventories","GetCharacterList","GetPlayerProfile",
            "GetPlayerStatistics","GetTitleData","GetUserAccountInfo","GetUserData",
            "GetUserInventory","GetUserReadOnlyData","GetUserVirtualCurrency"]}})
    # no data key means login failed
    if "data" not in r:
        log(f"Auth failed: {r}")
        return False
    session_ticket = r["data"]["SessionTicket"]
    entity_token = r["data"]["EntityToken"]["EntityToken"]
    # attach the ticket to all future playfab requests
    playfab.headers["X-Authorization"] = session_ticket
    # treat the login as valid for 6 hours
    auth_expiry = time.time() + (6 * 3600)
    log("Authentication successful")
    return True

# log in again only if the ticket has expired
def ensure_auth():
    if time.time() > auth_expiry:
        authenticate()

# mercury token
mercury_token = None
mercury_token_expiry = 0
ongoing_matches = {}  # match id to live match state
seen_match_ids = set()  # matches already handled

# get the match server token from a playfab cloudscript function
def fetch_mercury_token():
    global mercury_token, mercury_token_expiry
    ensure_auth()

    response = playfab_post("ExecuteCloudScript", {
        "FunctionName": "getUserTokenV1",
        "FunctionParameter": None,
        "GeneratePlayStreamEvent": None
    })

    # result sometimes comes back as a json string
    result = response.get("data", {}).get("FunctionResult")
    if isinstance(result, str):
        result = json.loads(result)

    if not result or not isinstance(result, dict) or "token" not in result:
        log(f"failed to get mercury token: {str(result)[:200]}")
        return False

    mercury_token = result["token"]
    # attach the token to all future match server requests
    match_repo.headers["x-auth-token"] = mercury_token
    # expire 5 minutes early or fall back to 6 hours
    try:
        valid_until = result.get("valid_until", "")
        expiry_dt = datetime.fromisoformat(valid_until)
        mercury_token_expiry = expiry_dt.timestamp() - 300
    except (ValueError, TypeError) as e:
        log(f"couldn't read mercury token expiry ({e}), assuming 6 hours")
        mercury_token_expiry = time.time() + (6 * 3600)
    log("mercury token obtained successfully")
    return True

# fetch a new mercury token if missing or expired
def ensure_mercury_token():
    if mercury_token is None or time.time() > mercury_token_expiry:
        fetch_mercury_token()

# player stat lookup
# get one stat for one player at any ladder rank
def fetch_stat_by_pfid(pfid, stat_name):
    ensure_auth()
    try:
        r = {}
        # retry up to 3 times on an empty reply
        for attempt in range(3):
            r = playfab_post("GetLeaderboardAroundPlayer", {
                "StatisticName": stat_name, "PlayFabId": pfid, "MaxResultsCount": 1})
            if r:
                break
            time.sleep(1)

        # pick out this player from the reply
        for entry in r.get("data", {}).get("Leaderboard", []):
            if entry.get("PlayFabId") == pfid:
                return entry.get("StatValue")
        return None
    except Exception as e:
        log(f"fetch_stat_by_pfid error for {pfid}/{stat_name}: {e}")
        return None

# find a tracked player id by display name or none
def find_pfid_by_name(players_db, display_name):
    # first case insensitive name match
    player = next(
        (p for p in players_db.values()
         if p.get("displayName", "").lower() == display_name.lower()),
        None
    )
    if not player:
        return None
    return player.get("playFabId")

# look up a player by display name then fetch their stat
def fetch_stat_for_player_by_name(display_name, stat_name):
    pfid = find_pfid_by_name(load_json(PLAYERS_FILE, {}), display_name)
    if not pfid:
        return None
    return fetch_stat_by_pfid(pfid, stat_name)

# get the account creation date for a player
# getaccountinfo returns this for any player unlike getplayerprofile
def fetch_account_created(pfid):
    ensure_auth()
    try:
        r = playfab_post("GetAccountInfo", {"PlayFabId": pfid})
        return r.get("data", {}).get("AccountInfo", {}).get("Created")
    except Exception as e:
        log(f"fetch_account_created error for {pfid}: {e}")
        return None

# match recording
# check the match server for newly started ranked matches
def poll_recent_matches():
    global mercury_token_expiry
    ensure_mercury_token()
    if not mercury_token:
        log("no mercury token, skipping recent matches poll")
        return

    try:
        response = match_repo.get(
            "https://blaston-match-repository-dot-blaston.ew.r.appspot.com/match-start-announcements/recent",
            timeout=REQUEST_TIMEOUT
        )
    except Exception as e:
        log(f"recent matches request failed: {e}")
        return

    # 401 means the token expired so force a refresh next time
    if response.status_code == 401:
        log("mercury token expired, refreshing")
        mercury_token_expiry = 0
        return
    elif response.status_code != 200:
        log(f"recent matches poll failed: {response.status_code}")
        return

    announcements = response.json().get("recent_announcements", [])
    # load tracked players once for the whole batch
    players_db = load_json(PLAYERS_FILE, {})

    for match in announcements:
        # skip matches already tracked or handled
        match_id = match.get("match_id")
        if not match_id or match_id in ongoing_matches or match_id in seen_match_ids:
            continue
        # classic ranked only
        if match.get("gameplay_rules") != "Classic":
            continue
        if match.get("match_making_queue") != "GlobalRatingPlay":
            continue
        p1_name = match.get("player_one_name")
        p2_name = match.get("player_two_name")
        if not p1_name or not p2_name:
            continue

        # skip quietly if either player isnt tracked
        p1_id = find_pfid_by_name(players_db, p1_name)
        p2_id = find_pfid_by_name(players_db, p2_name)
        if not p1_id or not p2_id:
            seen_match_ids.add(match_id)
            continue

        # mark the timestamp as utc
        ts = match.get("timestamp", "")
        if ts and not ts.endswith("Z"):
            ts = ts + "Z"

        # skip matches that started over 2 minutes ago
        # the rounds baseline would be wrong
        try:
            start = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - start).total_seconds() > 120:
                log(f"stale announcement for {p1_name} vs {p2_name}, skipping")
                seen_match_ids.add(match_id)
                continue
        except (ValueError, KeyError):
            pass

        # snapshot starting rounds and elo for both players
        p1_rounds = fetch_stat_by_pfid(p1_id, "ROUNDS_WON")
        time.sleep(0.2)
        p2_rounds = fetch_stat_by_pfid(p2_id, "ROUNDS_WON")
        time.sleep(0.2)
        p1_elo = fetch_stat_by_pfid(p1_id, "ELO_RATING")
        time.sleep(0.2)
        p2_elo = fetch_stat_by_pfid(p2_id, "ELO_RATING")

        # lookup failed so no baseline to score the match
        if p1_rounds is None or p2_rounds is None:
            log(f"couldn't get starting rounds for {p1_name} or {p2_name}, skipping match")
            seen_match_ids.add(match_id)
            continue

        # start tracking the match
        ongoing_matches[match_id] = {
            "match_id": match_id,
            "timestamp": ts,
            "player_one": p1_name,
            "player_two": p2_name,
            "player_one_id": p1_id,
            "player_two_id": p2_id,
            "player_one_lp": match.get("player_one_lp", 0),
            "player_two_lp": match.get("player_two_lp", 0),
            "player_one_rounds_start": p1_rounds or 0,
            "player_two_rounds_start": p2_rounds or 0,
            "player_one_rounds_won": 0,
            "player_two_rounds_won": 0,
            "player_one_elo": p1_elo,
            "player_two_elo": p2_elo,
            "started_at": time.time(),
        }
        log(f"new match: {p1_name} vs {p2_name} (id: {match_id[:8]}...)")


# check live matches for a finished result
def poll_ongoing_matches():
    ensure_auth()
    if not ongoing_matches:
        return

    completed = []
    # loop over a copy so entries can be removed after
    for match_id, match in list(ongoing_matches.items()):
        # give up after 500 seconds
        if time.time() - match["started_at"] > 500:
            log(f"match {match_id[:8]} timed out, abandoning")
            seen_match_ids.add(match_id)
            completed.append(match_id)
            continue

        # current lifetime rounds for both players
        time.sleep(0.5)
        p1_rounds_now = fetch_stat_by_pfid(match["player_one_id"], "ROUNDS_WON")
        time.sleep(0.5)
        p2_rounds_now = fetch_stat_by_pfid(match["player_two_id"], "ROUNDS_WON")

        if p1_rounds_now is None or p2_rounds_now is None:
            continue

        # rounds won this match
        p1_won = max(0, p1_rounds_now - match["player_one_rounds_start"])
        p2_won = max(0, p2_rounds_now - match["player_two_rounds_start"])

        match["player_one_rounds_won"] = p1_won
        match["player_two_rounds_won"] = p2_won

        # nobody on 2 yet so keep waiting
        if p1_won < 2 and p2_won < 2:
            continue

        # work out winner and loser
        if p1_won >= p2_won:
            winner, winner_won, loser_won = match["player_one"], p1_won, p2_won
        else:
            winner, winner_won, loser_won = match["player_two"], p2_won, p1_won

        # best of 3 check
        # winner must have exactly 2 and loser 0 or 1
        # anything else picked up rounds from another match
        if winner_won != 2 or loser_won > 1:
            log(f"match {match['match_id'][:8]} implausible counts "
                f"({p1_won}-{p2_won}), discarding")
            seen_match_ids.add(match_id)
            completed.append(match_id)
            continue

        total_rounds = winner_won + loser_won
        score = "2-0" if total_rounds == 2 else "2-1"
        save_match_from_announcement(match, winner, score, p1_won, p2_won)
        seen_match_ids.add(match_id)
        completed.append(match_id)
        log(f"match complete: {winner} wins {score}")

    # stop tracking finished matches
    for match_id in completed:
        del ongoing_matches[match_id]


# append a finished match to the matches file
def save_match_from_announcement(match, winner, score, p1_won, p2_won):
    try:
        existing = load_json(ALL_MATCHES_FILE, [])

        # skip if already saved
        if any(e.get("match_id") == match["match_id"] for e in existing):
            log(f"match {match['match_id'][:8]} already saved, skipping")
            return

        # guess round order from the totals
        round_winners = []
        p1_remaining = p1_won
        p2_remaining = p2_won
        for _ in range(p1_won + p2_won):
            if p1_remaining > p2_remaining:
                round_winners.append(match["player_one"])
                p1_remaining -= 1
            else:
                round_winners.append(match["player_two"])
                p2_remaining -= 1

        entry = {
            "match_id": match["match_id"],
            "timestamp": match["timestamp"],
            "winner": winner,
            "score": score,
            "roundCount": p1_won + p2_won,
            # rounds start lets rectify spot unrecorded games between matches
            "players": [
                {"displayName": match["player_one"], "leaguePoints": match["player_one_lp"],
                 "elo": match.get("player_one_elo"),
                 "roundsStart": match["player_one_rounds_start"]},
                {"displayName": match["player_two"], "leaguePoints": match["player_two_lp"],
                 "elo": match.get("player_two_elo"),
                 "roundsStart": match["player_two_rounds_start"]},
            ],
            "roundWinners": round_winners,
            "source": "announcement"
        }
        existing.append(entry)
        save_json(ALL_MATCHES_FILE, existing)
    except Exception as e:
        log(f"save_match_from_announcement error: {e}")

# season reset detection
# clear current season lp if the season version changed
def check_season_reset(current_version, players_db):
    stored = load_json(SEASON_FILE, {})
    last_version = stored.get("version")
    if last_version and last_version != current_version:
        log(f"Season reset detected! {last_version} -> {current_version}")
        for player in players_db.values():
            player["currentSeasonLP"] = None
            player["currentSeasonPosition"] = None
        return True
    return False

# leaderboard
# fetch both season lp ladders and update players
def poll_leaderboard():
    ensure_auth()
    log("Polling leaderboard...")
    r = playfab_post("ExecuteCloudScript", {
        "FunctionName": "getLeaguePointsSeasonV1",
        "FunctionParameter": None, "GeneratePlayStreamEvent": None})

    result = r.get("data", {}).get("FunctionResult")
    if not isinstance(result, dict):
        log("Leaderboard poll failed")
        return

    current = result.get("currentSeason", {})
    previous = result.get("previousSeason", {})

    # save the raw ladders
    save_json(LEADERBOARD_FILE, {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "currentSeason": {"version": current.get("version"), "leaderboard": current.get("leaderboard", [])},
        "previousSeason": {"version": previous.get("version"), "leaderboard": previous.get("leaderboard", [])}
    })

    players_db = load_json(PLAYERS_FILE, {})
    # clear out old history lists
    # charts are built from matches instead
    for p in players_db.values():
        p.pop("history", None)
    if check_season_reset(current.get("version"), players_db):
        save_json(PLAYERS_FILE, players_db)

    # remember the season version for next time
    save_json(SEASON_FILE, {"version": current.get("version"), "snapshotTime": datetime.now(timezone.utc).isoformat()})

    update_players_from_leaderboard(players_db, current.get("leaderboard", []), "current")
    update_players_from_leaderboard(players_db, previous.get("leaderboard", []), "previous")
    save_json(PLAYERS_FILE, players_db)
    log(f"Leaderboard saved: {len(current.get('leaderboard', []))} current, {len(previous.get('leaderboard', []))} previous")

# add new players and update lp and position from a ladder
def update_players_from_leaderboard(players_db, leaderboard, season):
    new_count = 0
    for entry in leaderboard:
        name = entry.get("displayName", "Unknown")
        pfid = entry.get("playFabMasterPlayerId")
        lp = entry.get("value", 0)
        position = entry.get("position", 0) + 1  # server ranks start at 0
        if name == "Unknown" or not pfid:
            continue
        # new player
        if pfid not in players_db:
            players_db[pfid] = {
                "displayName": name, "playFabId": pfid,
                "firstSeen": datetime.now(timezone.utc).isoformat(),
                "lastSeen": datetime.now(timezone.utc).isoformat(),
                "source": "leaderboard", "stats": {}
            }
            new_count += 1
        p = players_db[pfid]
        p["lastSeen"] = datetime.now(timezone.utc).isoformat()
        p["displayName"] = name
        # store lp under the right season
        if season == "current":
            p["currentSeasonLP"] = lp
            p["currentSeasonPosition"] = position
        else:
            p["previousSeasonLP"] = lp
            p["previousSeasonPosition"] = position
    if new_count > 0:
        log(f"Discovered {new_count} new players")

# update tracked player stats from each stat ladder
def refresh_all_stats(max_pages=30):
    ensure_auth()
    players_db = load_json(PLAYERS_FILE, {})

    # players with a real playfab id
    tracked = {pfid for pfid, p in players_db.items()
               if p.get("playFabId") and not pfid.startswith("name:")}
    now = datetime.now(timezone.utc).isoformat()
    touched = set()  # players updated this cycle
    missing_by_stat = {}  # stat name to tracked players not on the ladder

    for stat_name in STAT_NAMES:
        remaining = set(tracked)
        # page through the ladder 100 at a time
        for page in range(max_pages):
            try:
                r = playfab_post("GetLeaderboard", {
                    "StatisticName": stat_name,
                    "StartPosition": page * 100, "MaxResultsCount": 100})
            except Exception as e:
                log(f"refresh_all_stats {stat_name} page {page} error: {e}")
                break

            entries = r.get("data", {}).get("Leaderboard", [])
            if not entries:
                break

            for entry in entries:
                pfid = entry.get("PlayFabId")
                value = entry.get("StatValue")
                if not pfid or value is None:
                    continue

                # only add unknown players from the first page
                if pfid not in players_db:
                    if page != 0:
                        continue
                    players_db[pfid] = {
                        "displayName": entry.get("DisplayName", ""),
                        "playFabId": pfid,
                        "firstSeen": now, "lastSeen": now,
                        "source": "stats_leaderboard", "stats": {},
                    }

                p = players_db[pfid]
                p.setdefault("stats", {})
                # dont overwrite a real value with 0
                if value != 0 or stat_name not in p["stats"]:
                    p["stats"][stat_name] = value
                p["lastSeen"] = now
                p["statsLastUpdated"] = now
                touched.add(pfid)
                remaining.discard(pfid)

            # stop at the end of the ladder or once everyone is found
            if len(entries) < 100 or not remaining:
                break
            time.sleep(0.3)

        log(f"refresh_all_stats: {stat_name} done, {len(remaining)} tracked not on ladder")
        missing_by_stat[stat_name] = remaining

    # backfill off ladder players
    # one lookup per player and stat capped per cycle
    now_ts = time.time()

    # 2 for never fetched 1 for due a refresh 0 for tried recently
    def backfill_priority(pfid, stat):
        p = players_db.get(pfid, {})
        has = stat in p.get("stats", {})
        try:
            last = datetime.fromisoformat(
                (p.get("statsBackfilledAt") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            last = 0
        recent = (now_ts - last) <= BACKFILL_REFRESH_INTERVAL
        if recent:
            return 0
        return 2 if not has else 1

    # build the job list
    jobs = []
    for stat_name, remaining in missing_by_stat.items():
        for pfid in remaining:
            pr = backfill_priority(pfid, stat_name)
            if pr:
                jobs.append((pr, pfid, stat_name))
    # highest priority first
    jobs.sort(key=lambda j: j[0], reverse=True)

    calls = 0
    for _pr, pfid, stat_name in jobs:
        if calls >= BACKFILL_MAX_CALLS:
            break
        value = fetch_stat_by_pfid(pfid, stat_name)
        calls += 1
        p = players_db[pfid]
        p["statsBackfilledAt"] = now  # mark the attempt even if it failed
        time.sleep(0.15)
        if value is None:
            continue
        p.setdefault("stats", {})
        # dont overwrite a real value with 0
        if value != 0 or stat_name not in p["stats"]:
            p["stats"][stat_name] = value
        p["lastSeen"] = now
        p["statsLastUpdated"] = now
        touched.add(pfid)

    if jobs:
        log(f"refresh_all_stats: backfill made {calls} calls, "
            f"{len(jobs)} off-ladder stat values eligible this cycle")

    # only save if something changed
    if touched:
        save_json(PLAYERS_FILE, players_db)
    log(f"refresh_all_stats complete: {len(touched)} players updated")

# fetch account creation dates once per player
# failed lookups retry once per recheck interval
def backfill_account_ages(max_calls=ACCOUNT_AGE_MAX_CALLS):
    players_db = load_json(PLAYERS_FILE, {})
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = time.time()

    # needs a date and wasnt checked recently
    def eligible(p):
        if not p.get("playFabId") or p.get("accountCreated"):
            return False
        try:
            last = datetime.fromisoformat(
                (p.get("accountCheckedAt") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            last = 0
        return (now_ts - last) > ACCOUNT_RECHECK_INTERVAL

    pending = [pfid for pfid, p in players_db.items()
               if not pfid.startswith("name:") and eligible(p)]

    calls = got = 0
    for pfid in pending:
        if calls >= max_calls:
            break
        created = fetch_account_created(pfid)
        calls += 1
        p = players_db[pfid]
        p["accountCheckedAt"] = now_iso  # mark the attempt even if it failed
        if created:
            p["accountCreated"] = created
            got += 1
        time.sleep(0.15)

    if calls:
        save_json(PLAYERS_FILE, players_db)
        log(f"account age backfill: {got}/{calls} fetched "
            f"({len(pending)} pending, cap {max_calls})")

# run the full stats update
def poll_player_stats():
    log("Starting player stats update...")
    refresh_all_stats()
    backfill_account_ages()
    log("Player stats update complete")

# main
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    log("Blaston Poller Starting!")
    # cant do anything without a login
    if not authenticate():
        log("Auth failed, exiting")
        return

    # match polling needs the mercury token
    if not fetch_mercury_token():
        log("Warning: could not get mercury token, match polling will be skipped")

    # when each task last ran
    last_leaderboard = 0
    last_player_stats = 0
    last_recent_poll = 0

    while True:
        now = time.time()

        # check live matches every loop
        if ongoing_matches:
            try: poll_ongoing_matches()
            except Exception as e: log(f"ongoing matches error: {e}")

        # leaderboard every 5 minutes
        if now - last_leaderboard >= POLL_INTERVAL_LEADERBOARD:
            try: poll_leaderboard()
            except Exception as e: log(f"Leaderboard error: {e}")
            last_leaderboard = now

        # stats every 15 minutes
        if now - last_player_stats >= POLL_INTERVAL_PLAYER_STATS:
            try: poll_player_stats()
            except Exception as e: log(f"Stats error: {e}")
            # rerun the rectifier on the fresh elo
            try:
                n = rectify.rectify(DATA_DIR)
                log(f"rectify: {n} auto-corrections in corrections.json")
            except Exception as e: log(f"rectify error: {e}")
            last_player_stats = now

        # new matches every 30 seconds
        if now - last_recent_poll >= 30:
            try: poll_recent_matches()
            except Exception as e: log(f"recent matches error: {e}")
            last_recent_poll = now

        # sleep less while matches are live
        time.sleep(3 if ongoing_matches else 10)

if __name__ == "__main__":
    main()