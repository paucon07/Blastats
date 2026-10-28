from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Literal
import json, os, sqlite3, threading, traceback

import db


# app setup
app = FastAPI(title="Blaston Tracker API")
# allow requests from any site
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# make sure frontend is up to date
@app.middleware("http")
async def no_cache_frontend(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response

# file paths
DATA_DIR = db.DATA_DIR
ICONS_DIR = os.path.join(DATA_DIR, "icons")
AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
# stream info stays a file since it is edited by hand
STREAM_FILE = os.path.join(DATA_DIR, "stream_info.json")
# frontend files only
# keeps code and data off the public site
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# helpers
# read a json file or return default if missing or broken
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return default
    return default

# path to data and file mtime
_cache = {}

# mtime of a file or minus 1 if missing
def _mtime(path):
    return os.path.getmtime(path) if os.path.exists(path) else -1

# read a json file and reuse it until the file changes
def load_json_cached(path, default):
    mtime = _mtime(path)
    if path in _cache:
        data, cached_mtime = _cache[path]
        # file unchanged so reuse it
        if cached_mtime == mtime:
            return data
    data = load_json(path, default)
    _cache[path] = (data, mtime)
    return data

# page info for a list of total items
# 1 to 100 per page and the page kept in range
def _page(total, page, per_page):
    per_page = max(1, min(per_page, 100))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    return {"page": page, "perPage": per_page, "totalPages": total_pages, "total": total}

# database
# one connection per request since a connection cant be shared between threads
@contextmanager
def open_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()

# one long lived connection only used to notice writes from the poller
# data version changes whenever another connection commits
_watch = None
_watch_lock = threading.Lock()
_watch_version = None
_generation = 0

# number that goes up every time the database changes
# used as the key for the in memory caches
def data_generation():
    global _watch, _watch_version, _generation
    with _watch_lock:
        if _watch is None:
            # make sure the file and tables exist first
            db.connect().close()
            _watch = sqlite3.connect(db.DB_FILE, check_same_thread=False)
        version = _watch.execute("PRAGMA data_version").fetchone()[0]
        if version != _watch_version:
            _watch_version = version
            _generation += 1
        return _generation

# one page of ids from a query that selects ids first plus the page info
def _paged_ids(conn, sql, params, page, per_page):
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    info = _page(total, page, per_page)
    offset = (info["page"] - 1) * info["perPage"]
    ids = [r[0] for r in conn.execute(f"{sql} LIMIT ? OFFSET ?", (*params, info["perPage"], offset))]
    return ids, info

# players
# players with their season lp from the ladders
PLAYERS_SQL = """
SELECT p.*, s.current_season_lp, s.current_season_position,
    s.previous_season_lp, s.previous_season_position
FROM players p JOIN v_player_standings s ON s.player_id = p.player_id
"""

# player row and stats in the same shape as the old players json
def player_dict(row, stats):
    p = {"displayName": row["display_name"], "playFabId": row["player_id"],
         "firstSeen": row["first_seen"], "lastSeen": row["last_seen"],
         "source": row["source"], "stats": stats}
    # optional values only appear when set like before
    for key, col in (("currentSeasonLP", "current_season_lp"), ("currentSeasonPosition", "current_season_position"),
                     ("previousSeasonLP", "previous_season_lp"), ("previousSeasonPosition", "previous_season_position"),
                     ("accountCreated", "account_created"), ("accountCheckedAt", "account_checked_at"),
                     ("statsLastUpdated", "stats_updated_at"), ("statsBackfilledAt", "stats_backfilled_at")):
        if row[col] is not None:
            p[key] = row[col]
    return p

# find a player by name ignoring case or none
# the most recently added account wins when names are shared like before
def find_player(conn, name):
    row = conn.execute(PLAYERS_SQL + "WHERE p.display_name = ? COLLATE NOCASE ORDER BY p.rowid DESC LIMIT 1",
                       (name,)).fetchone()
    if row is None:
        return None
    stats = {s["stat_name"]: s["value"] for s in conn.execute(
        "SELECT stat_name, value FROM player_stats WHERE player_id = ?", (row["player_id"],))}
    return player_dict(row, stats)

# a players matches from their side newest first
# one row per match even when both sides share the name
# takes the name twice
PLAYER_MATCHES_SQL = """
SELECT pm.* FROM v_player_matches pm
WHERE pm.display_name = ? COLLATE NOCASE
AND pm.slot = (SELECT MIN(x.slot) FROM match_players x
               WHERE x.match_id = pm.match_id AND x.display_name = ? COLLATE NOCASE)
ORDER BY pm.started_at DESC, pm.match_id DESC
"""
# same query returning only the match ids
PLAYER_MATCH_IDS_SQL = PLAYER_MATCHES_SQL.replace("SELECT pm.*", "SELECT pm.match_id", 1)

# matches
# full matches in the same shape the old merged list had
# corrections are already applied by the views
def build_matches(conn, ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in conn.execute(f"SELECT * FROM v_matches WHERE id IN ({marks})", ids)}

    players = defaultdict(list)
    for p in conn.execute(f"SELECT * FROM match_players WHERE match_id IN ({marks}) ORDER BY match_id, slot", ids):
        players[p["match_id"]].append(p)
    weapons = defaultdict(list)
    for w in conn.execute(f"SELECT * FROM match_loadouts WHERE match_id IN ({marks}) "
                          "ORDER BY match_id, slot, position", ids):
        weapons[(w["match_id"], w["slot"])].append({"name": w["weapon"], "attachment": w["attachment"] or ""})
    rounds = defaultdict(list)
    for r in conn.execute(f"SELECT * FROM v_rounds WHERE match_id IN ({marks}) ORDER BY match_id, round_number", ids):
        rounds[r["match_id"]].append(r)
    # damage and grab totals by round then receiver then type then source
    stats = defaultdict(dict)
    for s in conn.execute(f"SELECT * FROM round_stats WHERE match_id IN ({marks})", ids):
        by_receiver = stats[(s["match_id"], s["round_number"])].setdefault(s["receiver_slot"], {})
        by_receiver.setdefault(s["stat_type"], {})[s["source"]] = s["value"]
    events = defaultdict(list)
    for e in conn.execute(f"SELECT * FROM round_events WHERE match_id IN ({marks}) "
                          "ORDER BY match_id, round_number, seq", ids):
        events[(e["match_id"], e["round_number"])].append(e)

    out = []
    for mid in ids:
        m = rows.get(mid)
        # excluded since the ids were picked
        if m is None:
            continue
        ps = players[mid]
        # a match always has two players so anything else is skipped not crashed on
        if len(ps) != 2:
            continue
        names = [p["display_name"] for p in ps]
        network_ids = [p["network_id"] for p in ps]
        spectated = bool(m["spectated"])

        match = {"timestamp": m["started_at"], "winner": names[m["winner_slot"]], "score": m["score"],
                 "roundCount": m["round_count"], "players": [],
                 "roundWinners": [names[r["winner_slot"]] for r in rounds[mid]], "spectated": spectated}
        match_id = m["spectate_id"] if spectated else m["announcement_id"]
        if match_id:
            match["match_id"] = match_id
        if m["corrected"]:
            match["corrected"] = True

        for p in ps:
            player = {"displayName": p["display_name"], "leaguePoints": p["league_points"]}
            if p["elo"] is not None:
                player["elo"] = p["elo"]
            if p["rounds_start"] is not None:
                player["roundsStart"] = p["rounds_start"]
            if spectated:
                for key, col in (("characterId", "character_id"), ("character", "character"),
                                 ("characterName", "character_name"), ("networkId", "network_id"), ("ping", "ping")):
                    if p[col] is not None:
                        player[key] = p[col]
                if p["network_id"] is not None:
                    player["isHost"] = p["network_id"] == 1
                player["weapons"] = weapons[(mid, p["slot"])]
            match["players"].append(player)

        if not spectated:
            match["source"] = "announcement"
            out.append(match)
            continue

        for key, col in (("matchEndTime", "ended_at"), ("endReason", "end_reason"), ("region", "region")):
            if m[col] is not None:
                match[key] = m[col]
        # round detail only exists on newer spectated matches
        match["rounds"] = []
        detail = []
        for r in rounds[mid]:
            n = r["round_number"]
            rnd = {"round": n, "winner": names[r["winner_slot"]]}
            if r["duration_seconds"] is not None:
                rnd["durationSeconds"] = r["duration_seconds"]
            if r["forfeit_slot"] is not None:
                rnd["endedByForfeit"] = True
                rnd["forfeitPlayerId"] = network_ids[r["forfeit_slot"]]
            if r["slot0_health"] is not None:
                rnd["p1Health"] = r["slot0_health"]
            if r["slot1_health"] is not None:
                rnd["p2Health"] = r["slot1_health"]
            # keyed by network id like the mod writes it
            if stats.get((mid, n)):
                rnd["stats"] = {str(network_ids[slot]): by_type for slot, by_type in stats[(mid, n)].items()}
            if events.get((mid, n)):
                rnd["events"] = [{"t": e["t"], "weapon": e["weapon"], "receiver": network_ids[e["receiver_slot"]],
                                  "dmg": e["dmg"]} for e in events[(mid, n)]]
            detail.append(rnd)
        if any(len(rnd) > 2 for rnd in detail):
            match["rounds"] = detail
        out.append(match)
    return out

# weapon stats
# weapon data is only on spectated matches
# corrections never touch those so facts only change when matches are added
# damage is stored by receiver so dealt damage comes from the other side
# throw and cage are not weapons
# frostbyte throws logged as plain throw are undercounted

# damage sources that are not weapons
NON_WEAPON_SOURCES = {"throw", "cage"}

# internal weapon id to display name
# loadouts use ids and damage uses display names
# keep in sync with the same map in app js
WEAPON_NAMES = {
    "Smg": "Wildcat", "Sniper": "Viper", "Shotgun": "Hammer", "Laser": "Lance",
    "Shield": "Cortex Shield", "Grenade": "Hammer Nade", "BigOrbus": "Nova",
    "Barrier": "Bunker Barrier", "FastLaser": "Lance Rapide", "BulletLauncherOrbus": "Nova Hurricane",
    "CurvedShotgun": "Hammer Ellipse", "CurvedSniper": "Viper Ellipse", "LaserGrenade": "Lance Nade",
    "AoeGrenade": "Avalanche Nade", "AoeBigOrb": "Majestic", "AoeGrenadeTwo": "Majestic Nade",
    "Handgun": "Raptor", "SpiritBomb": "Super Majestic", "FastShield": "Aegis Shield",
    "CurvedHandgun": "Raptor Ellipse", "OrbusGrenade": "Nova Nade", "Vandal": "Vandal",
    "ThrowableBall": "FrostByte", "LobberLauncher": "Hurricane", "TetherProjectileSpeed": "Quicksilver",
    "NovaHelix": "Nova Helix", "Vector": "Vector", "Swarm": "Swarm", "Bow": "Luma",
}
# any id or display name to display name
WEAPON_CANON = {}
for _int, _disp in WEAPON_NAMES.items():
    WEAPON_CANON[_int] = _disp
    WEAPON_CANON[_disp] = _disp

# display name for a weapon or the name unchanged
def _canon_weapon(k):
    return WEAPON_CANON.get(k, k)

# true unless it is throw or cage
def _is_weapon_source(key):
    return bool(key) and key.lower() not in NON_WEAPON_SOURCES

# frostbyte throw hits are always 5 15 or 20
# same check as the match detail view
# only used when the dealer had frostbyte
FROSTBYTE_THROW_DMG = (5, 15, 20)

# weapon facts in memory plus the spectated matches and database generation they came from
_wfacts_cache = None
_wfacts_cache_key = None
_wfacts_generation = None
# results per elo filter and exclude list
_weapon_agg_cache = {}

# weapon facts for the current database
# only rebuilt when spectated matches change
# not on every leaderboard or stats poll
def weapon_facts():
    global _wfacts_cache, _wfacts_cache_key, _wfacts_generation
    generation = data_generation()
    if _wfacts_cache is not None and _wfacts_generation == generation:
        return _wfacts_cache
    with open_db() as conn:
        # spectated matches are never edited so their count and newest id say if anything changed
        key = tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM matches WHERE spectated = 1").fetchone())
        if _wfacts_cache is None or _wfacts_cache_key != key:
            _wfacts_cache = _build_weapon_facts(conn)
            _wfacts_cache_key = key
    _wfacts_generation = generation
    return _wfacts_cache

# compact weapon record for every spectated match with a loadout newest first
# a few grouped queries instead of walking every match
def _build_weapon_facts(conn):
    facts = {}
    for m in conn.execute("SELECT id, winner_slot FROM matches WHERE spectated = 1 ORDER BY started_at DESC, id DESC"):
        facts[m["id"]] = {"elo": [None, None], "name": ["", ""], "winner": m["winner_slot"], "rounds": 0,
                          "sides": [{"loadout": [], "attach": [], "dmg": {}}, {"loadout": [], "attach": [], "dmg": {}}]}

    for p in conn.execute("SELECT mp.match_id, mp.slot, mp.display_name, mp.elo FROM match_players mp "
                          "JOIN matches m ON m.id = mp.match_id WHERE m.spectated = 1"):
        f = facts[p["match_id"]]
        f["name"][p["slot"]] = p["display_name"]
        f["elo"][p["slot"]] = float(p["elo"]) if p["elo"] is not None else None

    # unique weapons in loadout order plus every attachment
    for w in conn.execute("SELECT match_id, slot, weapon, attachment FROM match_loadouts ORDER BY match_id, slot, position"):
        if w["match_id"] not in facts:
            continue
        side = facts[w["match_id"]]["sides"][w["slot"]]
        if w["weapon"] not in side["loadout"]:
            side["loadout"].append(w["weapon"])
        if w["attachment"]:
            side["attach"].append(w["attachment"])

    # rounds from before damage tracking have no stats so dont count
    for r in conn.execute("SELECT match_id, COUNT(DISTINCT round_number) FROM round_stats GROUP BY match_id"):
        if r[0] in facts:
            facts[r[0]]["rounds"] = r[1]

    # damage is stored by receiver so dealt damage goes to the other side
    for s in conn.execute("SELECT match_id, receiver_slot, source, SUM(value) FROM round_stats "
                          "WHERE stat_type = 'Damage' GROUP BY match_id, receiver_slot, source"):
        match_id, receiver, source, total = s
        if match_id not in facts or not total or not _is_weapon_source(source):
            continue
        dmg = facts[match_id]["sides"][1 - receiver]["dmg"]
        k = _canon_weapon(source)
        dmg[k] = dmg.get(k, 0) + total

    # frostbyte throws are logged as plain throws so they are picked out by damage value
    # only rounds with stats count like the damage above
    marks = ",".join("?" * len(FROSTBYTE_THROW_DMG))
    for e in conn.execute(f"""
            SELECT e.match_id, e.receiver_slot, SUM(e.dmg) FROM round_events e
            WHERE lower(e.weapon) = 'throw' AND CAST(e.dmg + 0.5 AS INTEGER) IN ({marks})
            AND EXISTS (SELECT 1 FROM round_stats s WHERE s.match_id = e.match_id AND s.round_number = e.round_number)
            GROUP BY e.match_id, e.receiver_slot""", FROSTBYTE_THROW_DMG):
        match_id, receiver, total = e
        if match_id not in facts or not total:
            continue
        dealer = facts[match_id]["sides"][1 - receiver]
        frostbyte = next((w for w in dealer["loadout"] if w.lower() == "frostbyte"), None)
        if frostbyte:
            dealer["dmg"][frostbyte] = dealer["dmg"].get(frostbyte, 0) + total

    # no weapon data on either side
    return [f for f in facts.values() if f["sides"][0]["loadout"] or f["sides"][1]["loadout"]]

# lowest and highest elo seen
# bounds the frontend slider
def _ws_observed_elo_range(facts):
    lo, hi = None, None
    for m in facts:
        for e in m["elo"]:
            if e is None:
                continue
            lo = e if lo is None else min(lo, e)
            hi = e if hi is None else max(hi, e)
    return [lo, hi]

# per weapon stats for sides in the elo range
# both mode needs both players in range
# opponent mode only needs the opponent in range
# mirror matches are left out of the main win rate
# no non mirror games gives none instead of 50
# excluded players are skipped but still listed
def _aggregate_weapons(facts, min_elo, max_elo, mode, exclude=None):
    exclude = exclude or set()
    agg = {}
    # lowercase name to display name and entry count
    player_entries = {}

    # stats slot for a weapon made on first use
    def slot(w):
        return agg.setdefault(w, {
            "loadouts": 0, "wins_all": 0, "losses_all": 0,
            "wins_nm": 0, "losses_nm": 0, "mirrors": 0, "dmg": 0.0, "rounds": 0,
            "players": set(),
        })

    total_loadouts = 0
    matches_in_range = 0
    for m in facts:
        e = m["elo"]
        names = m["name"]
        winner = m["winner"]
        rounds = m["rounds"]
        sides = m["sides"]
        contributed = False
        for i in (0, 1):
            own, opp = e[i], e[1 - i]
            # elo filter
            if mode == "opponent":
                if opp is None or not (min_elo <= opp <= max_elo):
                    continue
            else:
                if own is None or opp is None:
                    continue
                if not (min_elo <= own <= max_elo and min_elo <= opp <= max_elo):
                    continue
            wielder = names[i]
            wkey = wielder.lower()
            # count the player even if excluded
            pe = player_entries.setdefault(wkey, [wielder, 0])
            pe[1] += 1
            if wkey in exclude:
                continue
            contributed = True
            total_loadouts += 1
            side = sides[i]
            opp_loadout = sides[1 - i]["loadout"]
            # none if the winner is unknown
            won = None if winner is None else (winner == i)
            for w in side["loadout"]:
                s = slot(w)
                s["loadouts"] += 1
                s["players"].add(wkey)
                s["dmg"] += side["dmg"].get(w, 0)
                s["rounds"] += rounds
                if won is not None:
                    if won:
                        s["wins_all"] += 1
                    else:
                        s["losses_all"] += 1
                    # both had it so mirror
                    if w in opp_loadout:
                        s["mirrors"] += 1
                    elif won:
                        s["wins_nm"] += 1
                    else:
                        s["losses_nm"] += 1
        if contributed:
            matches_in_range += 1

    # percent to 1 decimal or none
    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    # build the output rows
    weapons = []
    for w, s in agg.items():
        nm_games = s["wins_nm"] + s["losses_nm"]
        all_games = s["wins_all"] + s["losses_all"]
        weapons.append({
            "weapon": w,
            "loadouts": s["loadouts"],
            "players": len(s["players"]),
            "usage": pct(s["loadouts"], total_loadouts),
            "winRateNonMirror": pct(s["wins_nm"], nm_games),
            "winRateNonMirrorSample": nm_games,
            "winRateAll": pct(s["wins_all"], all_games),
            "winRateAllSample": all_games,
            "mirrors": s["mirrors"],
            "avgDmgPerRound": round(s["dmg"] / s["rounds"], 1) if s["rounds"] else None,
            "totalDamage": round(s["dmg"], 1),
            "dmgRounds": s["rounds"],
        })
    # most used first
    weapons.sort(key=lambda x: x["loadouts"], reverse=True)

    # players by entry count
    player_list = sorted(
        ({"name": disp, "entries": cnt} for disp, cnt in player_entries.values()),
        key=lambda x: (x["entries"], x["name"].lower()), reverse=True,
    )
    return {
        "mode": mode,
        "filter": {"minElo": min_elo, "maxElo": max_elo},
        "sampleLoadouts": total_loadouts,
        "sampleMatches": matches_in_range,
        "weapons": weapons,
        "playerList": player_list,
    }

# weapon stats for one player across all their spectated matches
# no elo filter
# use rate plus damage dealt and received per round
def player_weapon_stats(name, facts):
    nl = name.lower()
    used, dealt, recv = {}, {}, {}
    sample_matches = damage_matches = 0

    for m in facts:
        low = [m["name"][0].lower(), m["name"][1].lower()]
        # matches with this player only
        if nl not in low:
            continue
        # which side the player is on
        i = 0 if low[0] == nl else 1
        side, opp = m["sides"][i], m["sides"][1 - i]
        winner, rounds = m["winner"], m["rounds"]
        sample_matches += 1
        if rounds > 0:
            damage_matches += 1
        won = None if winner is None else (winner == i)
        # own weapons usage and damage dealt
        for w in side["loadout"]:
            u = used.setdefault(w, {"count": 0, "wins": 0, "losses": 0})
            u["count"] += 1
            if won is True:
                u["wins"] += 1
            elif won is False:
                u["losses"] += 1
            d = dealt.setdefault(w, {"dmg": 0.0, "rounds": 0})
            d["dmg"] += side["dmg"].get(w, 0)
            d["rounds"] += rounds
        # opponent weapons damage received
        for w in opp["loadout"]:
            rr = recv.setdefault(w, {"dmg": 0.0, "rounds": 0})
            rr["dmg"] += opp["dmg"].get(w, 0)
            rr["rounds"] += rounds

    # weapons by times used with win rate
    def most_used():
        out = []
        for w, u in used.items():
            g = u["wins"] + u["losses"]
            out.append({"weapon": w, "count": u["count"], "wins": u["wins"],
                        "losses": u["losses"],
                        "winRate": round(u["wins"] / g * 100, 1) if g else None})
        out.sort(key=lambda x: x["count"], reverse=True)
        return out

    # weapons by damage per round
    def dmg_list(src):
        out = []
        for w, d in src.items():
            # skip weapons with no damage
            if d["dmg"] <= 0 or d["rounds"] <= 0:
                continue
            out.append({"weapon": w, "damage": round(d["dmg"], 1), "rounds": d["rounds"],
                        "perRound": round(d["dmg"] / d["rounds"], 1)})
        out.sort(key=lambda x: x["perRound"], reverse=True)
        return out

    return {
        "sampleMatches": sample_matches,
        "damageMatches": damage_matches,
        "mostUsed": most_used(),
        "mostDamageDealt": dmg_list(dealt),
        "mostDamageReceived": dmg_list(recv),
    }

# player weapon stats against one opponent only
def matchup_weapon_stats(name, opponent, facts):
    nl, ol = name.lower(), opponent.lower()
    subset = []
    for m in facts:
        low = [m["name"][0].lower(), m["name"][1].lower()]
        # both players in it and not the same player twice
        if nl in low and ol in low and low[0] != low[1]:
            subset.append(m)
    if not subset:
        return None
    return player_weapon_stats(name, subset)

# routes
# api health and counts
@app.get("/status")
def get_status():
    with open_db() as conn:
        matches = conn.execute("SELECT COUNT(*) FROM v_matches").fetchone()[0]
        players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        updated = conn.execute("SELECT MAX(updated_at) FROM seasons").fetchone()[0]
    return {
        "status": "running",
        "matchesTracked": matches,
        "playersTracked": players,
        "leaderboardLastUpdated": updated,
    }

# current or previous season ladder
@app.get("/leaderboard")
def get_leaderboard(season: Literal["current", "previous"] = "current"):
    with open_db() as conn:
        updated = conn.execute("SELECT MAX(updated_at) FROM seasons").fetchone()[0]
        # poller hasnt written it yet
        if updated is None:
            raise HTTPException(status_code=503, detail="Leaderboard data not yet available")
        row = conn.execute("SELECT version FROM seasons WHERE status = ?", (season,)).fetchone()
        version = row["version"] if row else None
        entries = conn.execute("SELECT * FROM leaderboard_entries WHERE season_version = ? ORDER BY position",
                               (version,)).fetchall()
    leaderboard = []
    for e in entries:
        # positions are stored from 1 but the server sends them from 0
        entry = {"displayName": e["display_name"], "playFabMasterPlayerId": e["player_id"],
                 "position": e["position"] - 1, "value": e["value"]}
        if e["secondary_value"] is not None:
            entry["secondaryValue"] = e["secondary_value"]
        if e["formatted_value"] is not None:
            entry["formattedValue"] = e["formatted_value"]
        leaderboard.append(entry)
    return {
        "season": season,
        "version": version,
        "lastUpdated": updated,
        "leaderboard": leaderboard
    }

# player list sorted by lp or first seen
@app.get("/players")
def get_players(sort: str = "lp", limit: int = 500, streaks: bool = False):
    # rowid keeps ties in the order players were added like before
    # only these fixed strings ever go into the query
    order = {"lp": "COALESCE(s.current_season_lp, 0) DESC, p.rowid",
             "first_seen": "p.first_seen DESC, p.rowid"}.get(sort, "p.rowid")
    # cap the page size
    limit = max(1, min(limit, 1000))
    with open_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        if not total:
            raise HTTPException(status_code=503, detail="Player data not yet available")
        rows = conn.execute(PLAYERS_SQL + f"ORDER BY {order} LIMIT ?", (limit,)).fetchall()
        stats = defaultdict(dict)
        for s in conn.execute("SELECT * FROM player_stats"):
            stats[s["player_id"]][s["stat_name"]] = s["value"]
    sliced = [player_dict(r, stats[r["player_id"]]) for r in rows]

    # streaks only when asked for
    # other leaderboards skip the match scan
    if streaks:
        streak_map = all_streaks()
        enriched = []
        for pl in sliced:
            st = streak_map.get((pl.get("displayName") or "").lower())
            enriched.append({
                **pl,
                "currentStreak": st["current"] if st else 0,
                "streakType": st["type"] if st else None,
                "bestWinStreak": st["bestWin"] if st else 0,
                "bestLossStreak": st["bestLoss"] if st else 0,
            })
        sliced = enriched

    return {"total": total, "players": sliced}

# full lp and elo timeline for a player
@app.get("/players/{name}/lp-history")
def get_lp_history(name: str):
    with open_db() as conn:
        player = find_player(conn, name)
        if not player:
            raise HTTPException(status_code=404, detail=f"Player '{name}' not found")
        rows = conn.execute(PLAYER_MATCHES_SQL, (name, name)).fetchall()

    # built from the match log so it isnt capped
    history = []
    # oldest first
    for r in reversed(rows):
        entry = {"timestamp": r["started_at"]}
        if r["league_points"] is not None:
            entry["lp"] = r["league_points"]
        if r["elo"] is not None:
            entry["elo"] = r["elo"]
        # skip matches with neither value
        if "lp" in entry or "elo" in entry:
            history.append(entry)

    return {"displayName": player["displayName"], "history": history}

# round stats for a matchup from the name player side
# spectated matches only since they have round data
def _h2h_summary(name_l, h2h_matches):
    spectated = rounds = dur_n = hp_n = name_round_wins = 0
    dur_sum = hp_sum = 0.0
    for m in h2h_matches:
        # no round data
        rs = m.get("rounds") or []
        if not rs:
            continue
        players = m.get("players", [])
        if len(players) != 2:
            continue
        pn = [(p.get("displayName") or "").lower() for p in players]
        if name_l not in pn:
            continue
        # p1 health is the first player and p2 health the second
        idx = 0 if pn[0] == name_l else 1
        spectated += 1
        for r in rs:
            rounds += 1
            # round length
            d = r.get("durationSeconds")
            if isinstance(d, (int, float)) and d > 0:
                dur_sum += d
                dur_n += 1
            # health left over the opponent
            h1, h2 = r.get("p1Health"), r.get("p2Health")
            if isinstance(h1, (int, float)) and isinstance(h2, (int, float)):
                name_hp = h1 if idx == 0 else h2
                opp_hp = h2 if idx == 0 else h1
                hp_sum += (name_hp - opp_hp)
                hp_n += 1
            rw = r.get("winner")
            if rw and rw.lower() == name_l:
                name_round_wins += 1
    if not spectated:
        return None
    return {
        "spectatedMatches": spectated,
        "rounds": rounds,
        "avgRoundsPerMatch": round(rounds / spectated, 1) if spectated else None,
        "avgRoundTime": round(dur_sum / dur_n, 1) if dur_n else None,
        "avgHealthDiff": round(hp_sum / hp_n, 1) if hp_n else None,
        "roundWinRate": round(name_round_wins / rounds * 100, 1) if rounds else None,
    }

# head to head record between two players
@app.get("/players/{name}/vs/{opponent}")
def get_head_to_head(name: str, opponent: str):
    with open_db() as conn:
        # both players in the match only
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM v_player_matches WHERE display_name = ? COLLATE NOCASE "
            "AND opponent_name = ? COLLATE NOCASE GROUP BY match_id ORDER BY MAX(started_at) DESC, match_id DESC",
            (name, opponent))]
        # full matches so the frontend can show detail
        h2h = build_matches(conn, ids)
    name_wins, opponent_wins = 0, 0
    for match in h2h:
        winner = (match.get("winner") or "").lower()
        if winner == name.lower(): name_wins += 1
        elif winner == opponent.lower(): opponent_wins += 1
    total = name_wins + opponent_wins
    return {"playerOne": name, "playerTwo": opponent, "playerOneWins": name_wins,
            "playerTwoWins": opponent_wins, "totalMatches": total,
            "playerOneWinRate": round(name_wins / total * 100, 1) if total > 0 else 0,
            "matches": h2h,
            "summaryStats": _h2h_summary(name.lower(), h2h),
            "weaponStats": matchup_weapon_stats(name, opponent, weapon_facts())}

# tournament placements and prize money for a player
@app.get("/players/{name}/tournaments")
def get_player_tournaments(name: str):
    with open_db() as conn:
        rows = conn.execute(
            "SELECT t.name, p.place, p.prize_money FROM tournament_placements p "
            "JOIN tournaments t ON t.id = p.tournament_id WHERE p.display_name = ? COLLATE NOCASE ORDER BY t.id",
            (name,)).fetchall()
    results = [{"name": r["name"], "place": r["place"], "prizeMoney": r["prize_money"]} for r in rows]
    total = sum(r["prizeMoney"] for r in results)
    return {"results": results, "totalPrizeMoney": total}

# streaks from one player results newest first
# best streaks are the longest runs ever
# current streak runs from the latest match
# shared by the players list and the player profile
def _streaks(seq):
    # longest win and loss runs
    best_win = win_run = 0
    best_loss = loss_run = 0
    for r in seq:
        if r == "win":
            win_run += 1
            loss_run = 0
            best_win = max(best_win, win_run)
        else:
            loss_run += 1
            win_run = 0
            best_loss = max(best_loss, loss_run)
    # count back until the result changes
    current, ctype = 0, None
    for r in seq:
        if ctype is None:
            ctype = r
        if r != ctype:
            break
        current += 1
    return {"current": current, "type": ctype,
            "bestWin": best_win, "bestLoss": best_loss}

# streaks for every player in one pass
# rows are lowercase name and won newest first
def compute_all_streaks(rows):
    # lowercase name to results newest first
    seqs = defaultdict(list)
    for nm, won in rows:
        if nm:
            seqs[nm].append("win" if won else "loss")
    return {nm: _streaks(seq) for nm, seq in seqs.items()}

# streak map in memory and the database generation it came from
_streak_cache = None
_streak_cache_key = None

# streaks for every player rebuilt only when the database changes
def all_streaks():
    global _streak_cache, _streak_cache_key
    key = data_generation()
    if _streak_cache is None or _streak_cache_key != key:
        with open_db() as conn:
            _streak_cache = compute_all_streaks(conn.execute(
                "SELECT lower(display_name), won FROM v_player_matches ORDER BY started_at DESC, match_id DESC, slot"))
        _streak_cache_key = key
    return _streak_cache

# every match for a player newest first from their side
def build_player_matches(name, matches):
    name_l = name.lower()
    out = []
    for match in matches:
        player_names = [p.get("displayName") for p in match.get("players", [])]
        # skip matches without this player
        if name_l not in [n.lower() for n in player_names if n]:
            continue
        won = (match.get("winner") or "").lower() == name_l
        round_winners = match.get("roundWinners", [])
        opponent = next((p for p in match.get("players", [])
                         if (p.get("displayName") or "").lower() != name_l), None)
        my_player_data = next((pl for pl in match.get("players", [])
                               if (pl.get("displayName") or "").lower() == name_l), None)
        out.append({
            "timestamp": match.get("timestamp"),
            "result": "win" if won else "loss",
            "score": match.get("score"),
            "winner": match.get("winner"),
            "roundWinners": round_winners,
            "opponent": opponent.get("displayName") if opponent else "Unknown",
            "opponentLP": opponent.get("leaguePoints") if opponent else 0,
            "playerLP": my_player_data.get("leaguePoints") if my_player_data else None,
            "playerELO": my_player_data.get("elo") if my_player_data else None,
            "weapons": my_player_data.get("weapons", []) if my_player_data else [],
            "spectated": match.get("spectated", False),
            # spectated detail for the match view
            # missing on poller rows and older spectated rows
            "players": match.get("players", []),
            "rounds": match.get("rounds", []),
            "region": match.get("region"),
            "endReason": match.get("endReason"),
        })
    # newest first
    out.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
    return out

# full player profile
@app.get("/players/{name}")
def get_player(name: str):
    with open_db() as conn:
        player = find_player(conn, name)
        if not player:
            raise HTTPException(status_code=404, detail=f"Player '{name}' not found")
        rows = conn.execute(PLAYER_MATCHES_SQL, (name, name)).fetchall()

        # round totals from the player side of every match
        rounds_won, rounds_total = conn.execute(
            f"SELECT COALESCE(SUM(r.winner_slot = pm.slot), 0), COUNT(*) FROM ({PLAYER_MATCHES_SQL}) pm "
            "JOIN v_rounds r ON r.match_id = pm.match_id", (name, name)).fetchone()

        # latest avatar from the newest spectated match
        avatar = conn.execute(
            "SELECT mp.character FROM match_players mp JOIN v_matches m ON m.id = mp.match_id "
            "WHERE mp.display_name = ? COLLATE NOCASE AND mp.character IS NOT NULL "
            "ORDER BY m.started_at DESC, m.id DESC LIMIT 1", (name,)).fetchone()

        recent = build_player_matches(name, build_matches(conn, [r["match_id"] for r in rows[:20]]))

    # match totals
    wins = sum(1 for r in rows if r["won"])
    total = len(rows)

    # current and best streaks
    streaks = _streaks(["win" if r["won"] else "loss" for r in rows])

    # most played opponent
    # ties go to whoever was played most recently
    opponent_counts = {}
    for r in rows:
        opp = r["opponent_name"]
        if opp and opp.lower() != name.lower():
            opponent_counts[opp] = opponent_counts.get(opp, 0) + 1
    most_played = max(opponent_counts, key=opponent_counts.get) if opponent_counts else None

    return {
        **player,
        "wins": wins, "losses": total - wins,
        "winRate": round(wins / total * 100, 1) if total > 0 else 0,
        "roundsWon": rounds_won, "roundsLost": rounds_total - rounds_won,
        "matchesTracked": total,
        "mostPlayedOpponent": most_played,
        "currentStreak": streaks["current"],
        "streakType": streaks["type"],
        "bestWinStreak": streaks["bestWin"],
        "bestLossStreak": streaks["bestLoss"],
        "lastAvatar": avatar[0] if avatar else None,
        "recentMatches": recent,
        "weaponStats": player_weapon_stats(name, weapon_facts()),
    }

# one page of a player match history
@app.get("/players/{name}/matches")
def get_player_matches(name: str, page: int = 1, per_page: int = 20):
    with open_db() as conn:
        if not find_player(conn, name):
            raise HTTPException(status_code=404, detail=f"Player '{name}' not found")
        ids, info = _paged_ids(conn, PLAYER_MATCH_IDS_SQL,
                               (name, name), page, per_page)
        matches = build_matches(conn, ids)
    return {"matches": build_player_matches(name, matches), **info}

# global match log with optional player filter
@app.get("/matches")
def get_matches(limit: int = 100, player: str | None = None, page: int | None = None,
                per_page: int = 20):
    # cap the old mode size like the other routes
    limit = max(1, min(limit, 1000))
    if player:
        sql, params = PLAYER_MATCH_IDS_SQL, (player, player)
    else:
        sql, params = "SELECT id FROM v_matches ORDER BY started_at DESC, id DESC", ()

    with open_db() as conn:
        # paged mode when page is given
        # same shape as the player matches route
        if page is not None:
            ids, info = _paged_ids(conn, sql, params, page, per_page)
            return {"matches": build_matches(conn, ids), **info}

        # old mode for callers using only limit
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        ids = [r[0] for r in conn.execute(f"{sql} LIMIT ?", (*params, limit))]
        return {"total": total, "matches": build_matches(conn, ids)}


# stream info as stored
@app.get("/stream")
def get_stream():
    return load_json_cached(STREAM_FILE, {})

# tournaments in the same shape as the old json file
@app.get("/tournaments")
def get_tournaments():
    with open_db() as conn:
        tournaments = {t["id"]: {"id": t["id"], "name": t["name"], "placement": []}
                       for t in conn.execute("SELECT * FROM tournaments ORDER BY id")}
        for p in conn.execute("SELECT * FROM tournament_placements ORDER BY tournament_id, place"):
            tournaments[p["tournament_id"]]["placement"].append(
                {"displayName": p["display_name"], "place": p["place"], "prizeMoney": p["prize_money"]})
    return list(tournaments.values())

# total prize money per player highest first
@app.get("/leaderboard/earnings")
def get_earnings_leaderboard():
    with open_db() as conn:
        rows = conn.execute("SELECT display_name, total_earnings FROM v_earnings "
                            "ORDER BY total_earnings DESC, display_name").fetchall()
    return {"leaderboard": [{"displayName": r[0], "totalEarnings": r[1]} for r in rows]}

# global weapon stats for an elo range
# both mode is the bracket meta
# opponent mode is performance against the bracket
# exclude is a comma separated list of names
# no elo params gives the full range for the slider
@app.get("/weapons")
def get_weapons(min_elo: float | None = None, max_elo: float | None = None,
                mode: Literal["both", "opponent"] = "both", exclude: str | None = None):
    facts = weapon_facts()
    base_key = _wfacts_cache_key

    # default to the full observed range
    erange = _ws_observed_elo_range(facts)
    lo = min_elo if min_elo is not None else (erange[0] if erange[0] is not None else 0)
    hi = max_elo if max_elo is not None else (erange[1] if erange[1] is not None else 0)
    # swap if given backwards
    if lo > hi:
        lo, hi = hi, lo

    exclude_set = frozenset(n.strip().lower() for n in (exclude or "").split(",") if n.strip())

    # reuse a result for the same filters
    cache_key = (base_key, round(lo, 1), round(hi, 1), mode, exclude_set)
    cached = _weapon_agg_cache.get(cache_key)
    if cached is not None:
        return cached

    result = _aggregate_weapons(facts, lo, hi, mode, exclude_set)
    result["eloRange"] = erange
    result["totalSpectatedMatches"] = len(facts)

    # clear when too big since the slider makes lots of ranges
    if len(_weapon_agg_cache) > 128:
        _weapon_agg_cache.clear()
    _weapon_agg_cache[cache_key] = result
    return result

# match timestamps for the activity graph
@app.get("/activity")
def get_activity(window: str = "all", min_lp: int = 0):
    sql, params = "SELECT started_at FROM v_matches m WHERE 1 = 1", []
    # week window cutoff
    # timestamps are stored in one format so they compare as text
    if window == "week":
        sql += " AND m.started_at >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"))
    # needs a player at or above min lp
    if min_lp > 0:
        sql += " AND EXISTS (SELECT 1 FROM match_players p WHERE p.match_id = m.id AND COALESCE(p.league_points, 0) >= ?)"
        params.append(min_lp)

    # excluded matches are already left out by the view
    with open_db() as conn:
        timestamps = [r[0] for r in conn.execute(sql + " ORDER BY m.started_at DESC, m.id DESC", params)]
    return {"timestamps": timestamps, "total": len(timestamps), "window": window}

# error handlers
# http errors already come back as json by default
# anything else as a plain 500
@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    # log it so the cause isnt lost
    print(f"error on {request.url.path}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# static files
# make the folders so the mounts dont fail on startup
os.makedirs(AVATARS_DIR, exist_ok=True)
os.makedirs(ICONS_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATARS_DIR), name="avatars")
app.mount("/icons", StaticFiles(directory=ICONS_DIR), name="icons")
# frontend from the static folder only
# must be last since it catches every path
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
