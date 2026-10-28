import requests
import json
import os
import time
from datetime import datetime, timezone

import db
import rectify  # local match rectifier that writes corrections to the database
import store

# file paths
DATA_DIR = db.DATA_DIR
LOG_FILE = os.path.join(DATA_DIR, "poller.log")
# written by the mod and read here to pick up spectated matches
MATCHES_FILE = os.path.join(DATA_DIR, "matches.json")

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

# database
# one connection opened in main and shared by everything here
conn = None

# player fields that are timestamps and their database columns
PLAYER_TIMES = {"firstSeen": "first_seen", "lastSeen": "last_seen", "accountCreated": "account_created",
                "accountCheckedAt": "account_checked_at", "statsLastUpdated": "stats_updated_at",
                "statsBackfilledAt": "stats_backfilled_at"}

# players keyed by playfab id in the same shape the old json file had
# so the stat and ladder code below works on a plain dict
def load_players():
    players = {}
    for r in conn.execute("SELECT * FROM players ORDER BY rowid"):
        p = {"displayName": r["display_name"], "playFabId": r["player_id"], "source": r["source"], "stats": {}}
        for key, col in PLAYER_TIMES.items():
            if r[col] is not None:
                p[key] = r[col]
        players[r["player_id"]] = p
    for s in conn.execute("SELECT * FROM player_stats"):
        players[s["player_id"]]["stats"][s["stat_name"]] = s["value"]
    return players

# write every player and their stats back in one transaction
# first seen is never overwritten once a player exists
def save_players(players):
    rows, stats = [], []
    for pfid, p in players.items():
        times = {col: db.norm_ts(p.get(key)) for key, col in PLAYER_TIMES.items()}
        first = times["first_seen"] or db.now_iso()
        rows.append((pfid, p.get("displayName") or "", p.get("source") or "unknown", first,
                     times["last_seen"] or first, times["account_created"], times["account_checked_at"],
                     times["stats_updated_at"], times["stats_backfilled_at"]))
        stats += [(pfid, name, value) for name, value in (p.get("stats") or {}).items() if value is not None]
    with conn:
        conn.executemany("""
            INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (player_id) DO UPDATE SET
                display_name = excluded.display_name, source = excluded.source, last_seen = excluded.last_seen,
                account_created = excluded.account_created, account_checked_at = excluded.account_checked_at,
                stats_updated_at = excluded.stats_updated_at, stats_backfilled_at = excluded.stats_backfilled_at
        """, rows)
        conn.executemany("INSERT INTO player_stats VALUES (?, ?, ?) "
                         "ON CONFLICT (player_id, stat_name) DO UPDATE SET value = excluded.value", stats)

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
    pfid = find_pfid_by_name(load_players(), display_name)
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
    players_db = load_players()

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


# save a finished match to the database
# joins the spectated copy if the mod already saved it
def save_match_from_announcement(match, winner, score, p1_won, p2_won):
    try:
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
        # rolled back if it cant be saved
        with conn:
            store.add_announcement(conn, entry)
    except store.Skip as e:
        log(f"match {match['match_id'][:8]} not saved: {e}")
    except Exception as e:
        log(f"save_match_from_announcement error: {e}")

# season reset detection
# season lp is read from the ladder rows so nothing needs clearing
def check_season_reset(current_version):
    row = conn.execute("SELECT version FROM seasons WHERE status = 'current'").fetchone()
    last_version = row[0] if row else None
    if last_version and current_version and last_version != current_version:
        log(f"Season reset detected! {last_version} -> {current_version}")
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
    current_version = str(current["version"]) if current.get("version") is not None else None
    check_season_reset(current_version)

    # players first so every ladder row can link to one
    players_db = load_players()
    update_players_from_leaderboard(players_db, current.get("leaderboard", []))
    update_players_from_leaderboard(players_db, previous.get("leaderboard", []))
    save_players(players_db)
    save_ladders(current, previous)
    log(f"Leaderboard saved: {len(current.get('leaderboard', []))} current, {len(previous.get('leaderboard', []))} previous")

# add new players from a ladder and update their names
def update_players_from_leaderboard(players_db, leaderboard):
    new_count = 0
    now = db.now_iso()
    for entry in leaderboard:
        name = entry.get("displayName", "Unknown")
        pfid = entry.get("playFabMasterPlayerId")
        if name == "Unknown" or not pfid:
            continue
        # new player
        if pfid not in players_db:
            players_db[pfid] = {
                "displayName": name, "playFabId": pfid,
                "firstSeen": now, "lastSeen": now,
                "source": "leaderboard", "stats": {}
            }
            new_count += 1
        p = players_db[pfid]
        p["lastSeen"] = now
        p["displayName"] = name
    if new_count > 0:
        log(f"Discovered {new_count} new players")

# replace both season ladders and mark which season is current and previous
def save_ladders(current, previous):
    now = db.now_iso()
    known = {r[0] for r in conn.execute("SELECT player_id FROM players")}
    seen = set()
    with conn:
        # only one season can hold each status so they are cleared first
        conn.execute("UPDATE seasons SET status = NULL")
        for status, season in (("current", current), ("previous", previous)):
            if season.get("version") is None or str(season["version"]) in seen:
                continue
            version = str(season["version"])
            seen.add(version)
            conn.execute("INSERT INTO seasons VALUES (?, ?, ?, ?) ON CONFLICT (version) "
                         "DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                         (version, now, status, now))
            conn.execute("DELETE FROM leaderboard_entries WHERE season_version = ?", (version,))
            positions = set()
            for e in season.get("leaderboard", []):
                # server ranks start at 0
                position = e.get("position", 0) + 1
                if position in positions:
                    continue
                positions.add(position)
                pfid = e.get("playFabMasterPlayerId")
                conn.execute("INSERT INTO leaderboard_entries VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    version, position, pfid if pfid in known else None, e.get("displayName") or "Unknown",
                    e.get("value", 0), e.get("secondaryValue"), e.get("formattedValue")))

# update tracked player stats from each stat ladder
def refresh_all_stats(max_pages=30):
    ensure_auth()
    players_db = load_players()

    # players with a real playfab id
    tracked = {pfid for pfid, p in players_db.items()
               if p.get("playFabId") and not pfid.startswith("name:")}
    now = db.now_iso()
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
        save_players(players_db)
    log(f"refresh_all_stats complete: {len(touched)} players updated")

# fetch account creation dates once per player
# failed lookups retry once per recheck interval
def backfill_account_ages(max_calls=ACCOUNT_AGE_MAX_CALLS):
    players_db = load_players()
    now_iso = db.now_iso()
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
        save_players(players_db)
        log(f"account age backfill: {got}/{calls} fetched "
            f"({len(pending)} pending, cap {max_calls})")

# spectated matches
# the mod still writes matches json so new entries are copied into the database
# file mtime when it was last read
matches_mtime = None
# end time of the newest spectated match already copied
last_spectated = None

def ingest_spectated():
    global matches_mtime, last_spectated
    if not os.path.exists(MATCHES_FILE):
        return
    mtime = os.path.getmtime(MATCHES_FILE)
    if mtime == matches_mtime:
        return
    mods = load_json(MATCHES_FILE, None)
    # the mod rewrites the whole file so a half written one is read again next loop
    if not isinstance(mods, list):
        return
    if last_spectated is None:
        last_spectated = conn.execute("SELECT MAX(ended_at) FROM matches WHERE spectated = 1").fetchone()[0] or ""

    # the mod appends in the order matches end so only newer ones need looking at
    for m in mods:
        ended = db.norm_ts(m.get("matchEndTime"))
        if not ended or ended <= last_spectated:
            continue
        names = " vs ".join(p.get("displayName", "?") for p in m.get("players", []))
        try:
            # rolled back if it cant be saved
            with conn:
                store.add_spectated(conn, m)
            log(f"spectated match saved: {names}")
        except store.Skip as e:
            log(f"spectated match {names} not saved: {e}")
        last_spectated = ended
    matches_mtime = mtime

# run the full stats update
def poll_player_stats():
    log("Starting player stats update...")
    refresh_all_stats()
    backfill_account_ages()
    log("Player stats update complete")

# main
def main():
    global conn
    os.makedirs(DATA_DIR, exist_ok=True)
    log("Blaston Poller Starting!")
    conn = db.connect()
    # history from before the switch has to be imported first
    if not conn.execute("SELECT 1 FROM matches LIMIT 1").fetchone() and \
            os.path.exists(os.path.join(DATA_DIR, "all_matches.json")):
        log("Database has no matches yet, run import_json.py first to bring over the json history")
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
                n = rectify.rectify()
                log(f"rectify: {n} auto-corrections saved")
            except Exception as e: log(f"rectify error: {e}")
            last_player_stats = now

        # new matches every 30 seconds
        if now - last_recent_poll >= 30:
            try: poll_recent_matches()
            except Exception as e: log(f"recent matches error: {e}")
            last_recent_poll = now

        # spectated matches whenever the mod writes a new one
        try: ingest_spectated()
        except Exception as e: log(f"spectated matches error: {e}")

        # sleep less while matches are live
        time.sleep(3 if ongoing_matches else 10)

if __name__ == "__main__":
    main()