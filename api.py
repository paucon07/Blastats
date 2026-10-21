from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from collections import defaultdict
from typing import Literal
import json, os, traceback


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
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ICONS_DIR = os.path.join(DATA_DIR, "icons")
AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
LEADERBOARD_FILE = os.path.join(DATA_DIR, "leaderboard.json")
PLAYERS_FILE = os.path.join(DATA_DIR, "players.json")
MATCHES_FILE = os.path.join(DATA_DIR, "matches.json")
ALL_MATCHES_FILE = os.path.join(DATA_DIR, "all_matches.json")
CORRECTIONS_FILE = os.path.join(DATA_DIR, "corrections.json")
STREAM_FILE = os.path.join(DATA_DIR, "stream_info.json")
TOURNAMENTS_FILE = os.path.join(DATA_DIR, "tournaments.json")
WEAPON_FACTS_FILE = os.path.join(DATA_DIR, "weapon_facts_cache.json")
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

# write to a temp file then swap it in
# readers never see a half written file
def save_json(path, data):
    # temp name per process so workers dont clash
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)

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

# mtimes of both match files
# used as the cache key for merges and weapon facts
def _match_files_key():
    matches_mtime = os.path.getmtime(MATCHES_FILE) if os.path.exists(MATCHES_FILE) else 0
    all_mtime = os.path.getmtime(ALL_MATCHES_FILE) if os.path.exists(ALL_MATCHES_FILE) else 0
    return (matches_mtime, all_mtime)

# one page of a list plus page info
# 1 to 100 per page and the page kept in range
def _paginate(items, page, per_page):
    per_page = max(1, min(per_page, 100))
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return {
        "matches": items[start:start + per_page],
        "page": page,
        "perPage": per_page,
        "totalPages": total_pages,
        "total": total,
    }

# players dict the name index was built from
_name_index_src = None
_name_index = {}

# find a player by name ignoring case or none
# index is rebuilt only when players json reloads
def find_player(players, name):
    global _name_index_src, _name_index
    if players is not _name_index_src:
        _name_index = {(p.get("displayName") or "").lower(): p for p in players.values()}
        _name_index_src = players
    return _name_index.get(name.lower())

# match validation
# check a match is a real best of 3 result
def is_valid_match(match):
    players = match.get("players", [])
    winner = match.get("winner", "")
    round_winners = match.get("roundWinners", [])
    # needs 2 players and a winner
    if len(players) != 2 or not winner:
        return False
    # 2 or 3 rounds only
    n = len(round_winners)
    if n < 2 or n > 3:
        return False
    # winner took exactly 2 and loser at most 1
    # filters old 4 round rows from the poller
    winner_rounds = sum(1 for r in round_winners if (r or "").lower() == winner.lower())
    return winner_rounds == 2 and (n - winner_rounds) <= 1

# merged match list before corrections
_merged_cache = None
# match file mtimes the merged list was built from
_merged_cache_key = None

# on disk copy of the merged list
# old merged cache file is ignored since the format changed
MERGED_CACHE_FILE = os.path.join(DATA_DIR, "merged_base_cache.json")

# corrections file contents and its mtime
_corr_cache = None
_corr_cache_mtime = -1

# read corrections only when the file changes
# poller rewrites it every stats cycle
def load_corrections_cached():
    global _corr_cache, _corr_cache_mtime
    mtime = _mtime(CORRECTIONS_FILE)
    if _corr_cache is None or mtime != _corr_cache_mtime:
        _corr_cache = load_json(CORRECTIONS_FILE, {})
        _corr_cache_mtime = mtime
    return _corr_cache

# corrections
# apply winner flips and exclusions on top of the matches
# data files are never changed
# rectify writes the corrections file
# hand added entries need source manual so rectify keeps them
def apply_corrections(matches):
    corr = load_corrections_cached()
    if not corr:
        return matches
    out = []
    for m in matches:
        # never correct a spectated match
        # it can share a match id with a corrected poller row
        if m.get("spectated"):
            out.append(m)
            continue
        c = corr.get(m.get("match_id"))
        # no correction for this match
        if not c:
            out.append(m)
            continue
        # drop the match
        if c.get("exclude"):
            continue
        old_w = m.get("winner") or ""
        names = [p.get("displayName") for p in m.get("players", [])]
        # new winner must be one of the two players
        # also fixes the name casing
        new_w = next((n for n in names if n and n.lower() == (c.get("winner") or "").lower()), None)
        if new_w and new_w.lower() != old_w.lower():
            other = next((n for n in names if n and n.lower() != new_w.lower()), None)
            # swap every round winner to match
            rw = [new_w if (r or "").lower() == old_w.lower() else other
                  for r in m.get("roundWinners", [])]
            m = {**m, "winner": new_w, "roundWinners": rw, "corrected": True}
        out.append(m)
    return out


# merging
# full match list newest first with corrections applied
def merge_matches():
    # slow merge is cached on match file mtimes only
    base = _build_base_merged(_match_files_key())
    # corrections are cheap so apply every request
    return apply_corrections(base)


# pair spectated matches with poller announcements
# cached in memory and on disk
def _build_base_merged(base_key):
    global _merged_cache, _merged_cache_key

    # match files unchanged so reuse the memory copy
    if _merged_cache is not None and _merged_cache_key == base_key:
        return _merged_cache

    # newest match file change
    source_key = max(base_key)

    # disk copy is newer than both match files so reuse it
    # shared across workers and restarts
    cache_mtime = os.path.getmtime(MERGED_CACHE_FILE) if os.path.exists(MERGED_CACHE_FILE) else 0
    if cache_mtime > source_key:
        _merged_cache = load_json(MERGED_CACHE_FILE, [])
        _merged_cache_key = base_key
        return _merged_cache

    # read fresh copies since the data changed
    # cached reads could be stale
    mod_matches = load_json(MATCHES_FILE, [])
    all_matches = load_json(ALL_MATCHES_FILE, [])

    # group announcements by player pair
    ann_by_players = defaultdict(list)
    for ann in all_matches:
        try:
            key = frozenset((p.get("displayName") or "").lower() for p in ann.get("players", []))
            ann_time = datetime.fromisoformat(ann["timestamp"].replace("Z", "+00:00"))
            ann_by_players[key].append((ann_time, ann))
        except (ValueError, KeyError):
            continue

    # valid spectated matches oldest first
    sorted_mods = sorted(
        (m for m in mod_matches if is_valid_match(m)),
        key=lambda m: m.get("timestamp", "")
    )

    merged = []
    for mod_match in sorted_mods:
        try:
            mod_time = datetime.fromisoformat(mod_match["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            # bad timestamp so keep it unpaired
            merged.append({**mod_match, "spectated": True})
            continue

        key = frozenset((p.get("displayName") or "").lower() for p in mod_match.get("players", []))

        # closest announcement for the same pair within 3 minutes
        best_idx, best_dt = None, float("inf")
        for i, (ann_time, _) in enumerate(ann_by_players[key]):
            dt = abs((mod_time - ann_time).total_seconds())
            if dt < 180 and dt < best_dt:
                best_idx, best_dt = i, dt

        if best_idx is not None:
            # remove it so it cant pair twice
            _, best_ann = ann_by_players[key].pop(best_idx)
            # copy elo from the announcement
            ann_elo: dict[str, object] = {
                (p.get("displayName") or "").lower(): p.get("elo")
                for p in best_ann.get("players", [])
            }
            # keep the player as is when the announcement has no elo
            merged_players = []
            for p in mod_match.get("players", []):
                pname: str = (p.get("displayName") or "").lower()
                elo = ann_elo.get(pname)
                merged_players.append({**p, "elo": elo} if elo is not None else p)
            # use the announcement timestamp
            merged_match = {**mod_match, "timestamp": best_ann["timestamp"],
                            "spectated": True, "players": merged_players}
        else:
            merged_match = {**mod_match, "spectated": True}
        merged.append(merged_match)

    # merged match times by player pair
    merged_times_by_players = defaultdict(list)
    for m in merged:
        key = frozenset((p.get("displayName") or "").lower() for p in m.get("players", []))
        try:
            merged_times_by_players[key].append(
                datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            )
        except (ValueError, KeyError):
            pass

    # add leftover announcements as non spectated
    for ann_key, ann_entries in ann_by_players.items():
        for ann_time, ann in ann_entries:
            # skip if a merged match is within 3 minutes
            already = any(
                abs((ann_time - mt).total_seconds()) < 180
                for mt in merged_times_by_players[ann_key]
            )
            if not already:
                merged.append({**ann, "spectated": False})

    # newest first
    # corrections are not baked in
    result = sorted(merged, key=lambda m: m.get("timestamp") or "", reverse=True)

    # save to memory and disk
    _merged_cache = result
    _merged_cache_key = base_key
    # disk write failing just means a rebuild next time
    try:
        save_json(MERGED_CACHE_FILE, result)
    except OSError as e:
        print(f"merged cache write failed: {e}")

    return result

# weapon stats
# weapon data is only on spectated matches
# corrections never touch those so facts only depend on match file mtimes
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

# weapon list from weapons or deck
# same check as the frontend
def _ws_weapon_entries(pl):
    return pl.get("weapons") or pl.get("deck") or []

# unique weapon display names for a player
def _ws_loadout(pl):
    seen = []
    for w in _ws_weapon_entries(pl):
        nm = w.get("name")
        if not nm:
            continue
        nm = _canon_weapon(nm)
        if nm not in seen:
            seen.append(nm)
    return seen

# attachments on a player weapons
def _ws_attachments(pl):
    out = []
    for w in _ws_weapon_entries(pl):
        a = w.get("attachment")
        if a:
            out.append(a)
    return out

# player elo as a float or none
def _ws_elo(pl):
    e = pl.get("elo")
    try:
        return float(e) if e is not None else None
    except (TypeError, ValueError):
        return None

# frostbyte throw hits are always 5 15 or 20
# same check as the match detail view
# only used when the dealer had frostbyte
FROSTBYTE_THROW_DMG = {5, 15, 20}

# frostbyte name as spelled in the loadout or none
def _loadout_frostbyte_name(loadout):
    for w in loadout:
        if isinstance(w, str) and w.lower() == "frostbyte":
            return w
    return None

# round half up like js
# so 14 point 5 becomes 15 like the frontend
def _js_round(x):
    return int(x + 0.5)

# total frostbyte throw damage a player received in a round
def _frostbyte_throw_dmg(rnd, receiver_nid):
    total = 0.0
    rid = str(receiver_nid)
    for e in (rnd.get("events") or []):
        # hits on this receiver only
        if not isinstance(e, dict) or str(e.get("receiver")) != rid:
            continue
        # throws only
        if (e.get("weapon") or "").lower() != "throw":
            continue
        d = e.get("dmg")
        if d is None:
            continue
        try:
            dv = float(d)
        except (TypeError, ValueError):
            continue
        # frostbyte damage values only
        if _js_round(dv) in FROSTBYTE_THROW_DMG:
            total += dv
    return total

# compact weapon record for one spectated match or none
def _extract_match_fact(m):
    players = m.get("players", [])
    if len(players) != 2:
        return None
    p0, p1 = players[0], players[1]
    l0, l1 = _ws_loadout(p0), _ws_loadout(p1)
    # no weapon data on this row
    if not l0 and not l1:
        return None

    fb0, fb1 = _loadout_frostbyte_name(l0), _loadout_frostbyte_name(l1)
    # dmg0 is damage dealt by side 0 so side 1 received bucket
    dmg0, dmg1 = {}, {}
    rounds_counted = 0
    nid0, nid1 = p0.get("networkId"), p1.get("networkId")
    if nid0 is not None and nid1 is not None:
        s0, s1 = str(nid0), str(nid1)
        for r in (m.get("rounds") or []):
            stats = r.get("stats") or {}
            recv0 = stats.get(s0) or {}
            recv1 = stats.get(s1) or {}
            # round is from before damage tracking
            if not recv0 and not recv1:
                continue
            rounds_counted += 1
            # damage side 0 dealt
            for wpn, amt in (recv1.get("Damage") or {}).items():
                if amt and _is_weapon_source(wpn):
                    k = _canon_weapon(wpn)
                    dmg0[k] = dmg0.get(k, 0) + amt
            # damage side 1 dealt
            for wpn, amt in (recv0.get("Damage") or {}).items():
                if amt and _is_weapon_source(wpn):
                    k = _canon_weapon(wpn)
                    dmg1[k] = dmg1.get(k, 0) + amt
            # side 1 frostbyte throws into side 0
            if fb1:
                f = _frostbyte_throw_dmg(r, nid0)
                if f:
                    dmg1[fb1] = dmg1.get(fb1, 0) + f
            # side 0 frostbyte throws into side 1
            if fb0:
                f = _frostbyte_throw_dmg(r, nid1)
                if f:
                    dmg0[fb0] = dmg0.get(fb0, 0) + f

    d0 = p0.get("displayName") or ""
    d1 = p1.get("displayName") or ""
    # winner as side 0 or 1 or none
    wl = (m.get("winner") or "").lower()
    winner = 0 if (wl and wl == d0.lower()) else (1 if (wl and wl == d1.lower()) else None)

    return {
        "elo": [_ws_elo(p0), _ws_elo(p1)],
        "name": [d0, d1],
        "winner": winner,
        "rounds": rounds_counted,
        "sides": [
            {"loadout": l0, "attach": _ws_attachments(p0), "dmg": dmg0},
            {"loadout": l1, "attach": _ws_attachments(p1), "dmg": dmg1},
        ],
    }

# weapon facts in memory and the mtimes they came from
_wfacts_cache = None
_wfacts_cache_key = None
# results per elo filter and exclude list
_weapon_agg_cache = {}

# bump when the fact format changes so old disk caches are dropped
# v5 matches frostbyte in any casing
WEAPON_FACTS_VERSION = 5

# weapon facts for the current match files
def weapon_facts():
    return _build_weapon_facts(_match_files_key())

# facts for every usable spectated match
# cached in memory and on disk like the merge
def _build_weapon_facts(base_key):
    global _wfacts_cache, _wfacts_cache_key
    # match files unchanged so reuse the memory copy
    if _wfacts_cache is not None and _wfacts_cache_key == base_key:
        return _wfacts_cache

    # disk copy is newer and the same version so reuse it
    source_key = max(base_key)
    cache_mtime = os.path.getmtime(WEAPON_FACTS_FILE) if os.path.exists(WEAPON_FACTS_FILE) else 0
    if cache_mtime > source_key:
        cached = load_json(WEAPON_FACTS_FILE, None)
        if isinstance(cached, dict) and cached.get("version") == WEAPON_FACTS_VERSION:
            _wfacts_cache = cached.get("facts", [])
            _wfacts_cache_key = base_key
            return _wfacts_cache

    # merged list has elo attached and no corrections
    base = _build_base_merged(base_key)
    facts = []
    for m in base:
        # spectated matches only
        if not m.get("spectated"):
            continue
        f = _extract_match_fact(m)
        if f is not None:
            facts.append(f)

    # save to memory and disk
    _wfacts_cache = facts
    _wfacts_cache_key = base_key
    try:
        save_json(WEAPON_FACTS_FILE, {"version": WEAPON_FACTS_VERSION, "facts": facts})
    except OSError as e:
        print(f"weapon facts cache write failed: {e}")
    return facts

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
    leaderboard = load_json_cached(LEADERBOARD_FILE, {})
    players = load_json_cached(PLAYERS_FILE, {})
    matches = merge_matches()
    return {
        "status": "running",
        "matchesTracked": len(matches),
        "playersTracked": len(players),
        "leaderboardLastUpdated": leaderboard.get("lastUpdated"),
    }

# current or previous season ladder
@app.get("/leaderboard")
def get_leaderboard(season: Literal["current", "previous"] = "current"):
    leaderboard = load_json_cached(LEADERBOARD_FILE, {})
    # poller hasnt written it yet
    if not leaderboard:
        raise HTTPException(status_code=503, detail="Leaderboard data not yet available")
    # plain str hint stops a false LiteralString warning in pycharm
    key: str = "currentSeason" if season == "current" else "previousSeason"
    data = leaderboard.get(key, {})
    return {
        "season": season,
        "version": data.get("version"),
        "lastUpdated": leaderboard.get("lastUpdated"),
        "leaderboard": data.get("leaderboard", [])
    }

# player list sorted by lp or first seen
@app.get("/players")
def get_players(sort: str = "lp", limit: int = 500, streaks: bool = False):
    players = load_json_cached(PLAYERS_FILE, {})
    if not players:
        raise HTTPException(status_code=503, detail="Player data not yet available")
    player_list = list(players.values())
    if sort == "lp":
        player_list.sort(key=lambda p: p.get("currentSeasonLP") or 0, reverse=True)
    elif sort == "first_seen":
        player_list.sort(key=lambda p: p.get("firstSeen") or "", reverse=True)

    # cap the page size
    limit = max(1, min(limit, 1000))
    sliced = player_list[:limit]

    # streaks only when asked for
    # other leaderboards skip the match scan
    if streaks:
        streak_map = compute_all_streaks(merge_matches())
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

    return {"total": len(player_list), "players": sliced}

# full lp and elo timeline for a player
@app.get("/players/{name}/lp-history")
def get_lp_history(name: str):
    players = load_json_cached(PLAYERS_FILE, {})
    player = find_player(players, name)
    if not player:
        raise HTTPException(status_code=404, detail=f"Player '{name}' not found")

    # built from the match log so it isnt capped
    # unlike the trimmed history snapshots
    player_matches = build_player_matches(name, merge_matches())
    history = []
    # oldest first
    for m in reversed(player_matches):
        entry = {"timestamp": m.get("timestamp")}
        if m.get("playerLP") is not None:
            entry["lp"] = m["playerLP"]
        if m.get("playerELO") is not None:
            entry["elo"] = m["playerELO"]
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
    matches = merge_matches()
    h2h, name_wins, opponent_wins = [], 0, 0
    for match in matches:
        names = [(p.get("displayName") or "").lower() for p in match.get("players", [])]
        # both players in the match only
        if name.lower() not in names or opponent.lower() not in names:
            continue
        winner = (match.get("winner") or "").lower()
        if winner == name.lower(): name_wins += 1
        elif winner == opponent.lower(): opponent_wins += 1
        # full match so the frontend can show detail
        h2h.append(match)
    # newest first
    h2h.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
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
    tournaments = load_json_cached(TOURNAMENTS_FILE, [])
    results = []
    for t in tournaments:
        for p in t.get("placement", []):
            if (p.get("displayName") or "").lower() == name.lower():
                # get so one incomplete entry cant crash the route
                results.append({
                    "name": t.get("name"),
                    "place": p.get("place"),
                    "prizeMoney": p.get("prizeMoney") or 0
                })
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
# matches are newest first
def compute_all_streaks(matches):
    # lowercase name to results newest first
    seqs = defaultdict(list)
    for match in matches:
        winner = (match.get("winner") or "").lower()
        for pl in match.get("players", []):
            nm = (pl.get("displayName") or "").lower()
            if nm:
                seqs[nm].append("win" if nm == winner else "loss")
    return {nm: _streaks(seq) for nm, seq in seqs.items()}

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
    players = load_json_cached(PLAYERS_FILE, {})
    matches = merge_matches()
    player = find_player(players, name)
    if not player:
        raise HTTPException(status_code=404, detail=f"Player '{name}' not found")

    name_l = name.lower()
    player_matches = build_player_matches(name, matches)
    # match and round totals
    wins = sum(1 for m in player_matches if m["result"] == "win")
    losses = len(player_matches) - wins
    rounds_won = sum(
        sum(1 for r in m["roundWinners"] if (r or "").lower() == name_l)
        for m in player_matches
    )
    rounds_lost = sum(len(m["roundWinners"]) for m in player_matches) - rounds_won
    total = len(player_matches)

    # current and best streaks
    streaks = _streaks([m["result"] for m in player_matches])

    # most played opponent
    opponent_counts = {}
    for m in player_matches:
        opp = m.get("opponent")
        if opp and opp != "Unknown":
            opponent_counts[opp] = opponent_counts.get(opp, 0) + 1
    most_played = max(opponent_counts, key=opponent_counts.get) if opponent_counts else None

    # latest avatar from the newest spectated match
    last_avatar = None
    for m in player_matches:
        for pl in m.get("players", []):
            if (pl.get("displayName") or "").lower() == name_l and pl.get("character"):
                last_avatar = pl["character"]
                break
        if last_avatar:
            break

    return {
        **player,
        "wins": wins, "losses": losses,
        "winRate": round(wins / total * 100, 1) if total > 0 else 0,
        "roundsWon": rounds_won, "roundsLost": rounds_lost,
        "matchesTracked": total,
        "mostPlayedOpponent": most_played,
        "currentStreak": streaks["current"],
        "streakType": streaks["type"],
        "bestWinStreak": streaks["bestWin"],
        "bestLossStreak": streaks["bestLoss"],
        "lastAvatar": last_avatar,
        "recentMatches": player_matches[:20],
        "weaponStats": player_weapon_stats(name, weapon_facts()),
    }

# one page of a player match history
@app.get("/players/{name}/matches")
def get_player_matches(name: str, page: int = 1, per_page: int = 20):
    players = load_json_cached(PLAYERS_FILE, {})
    if not find_player(players, name):
        raise HTTPException(status_code=404, detail=f"Player '{name}' not found")

    return _paginate(build_player_matches(name, merge_matches()), page, per_page)

# global match log with optional player filter
@app.get("/matches")
def get_matches(limit: int = 100, player: str | None = None, page: int | None = None,
                per_page: int = 20):
    # cap the old mode size like the other routes
    limit = max(1, min(limit, 1000))
    matches = merge_matches()
    if player:
        matches = [m for m in matches if any(
            (p.get("displayName") or "").lower() == player.lower()
            for p in m.get("players", []))]
    total = len(matches)

    # paged mode when page is given
    # same shape as the player matches route
    if page is not None:
        return _paginate(matches, page, per_page)

    # old mode for callers using only limit
    return {"total": total, "matches": matches[:limit]}


# stream info as stored
@app.get("/stream")
def get_stream():
    return load_json_cached(STREAM_FILE, {})

# tournaments as stored
@app.get("/tournaments")
def get_tournaments():
    return load_json_cached(TOURNAMENTS_FILE, [])

# total prize money per player highest first
@app.get("/leaderboard/earnings")
def get_earnings_leaderboard():
    tournaments = load_json_cached(TOURNAMENTS_FILE, [])
    earnings = {}
    for t in tournaments:
        for p in t.get("placement", []):
            name = p.get("displayName")
            prize = p.get("prizeMoney", 0)
            if name and prize:
                earnings[name] = earnings.get(name, 0) + prize
    ranked = sorted(
        [{"displayName": name, "totalEarnings": total} for name, total in earnings.items()],
        key=lambda x: x["totalEarnings"],
        reverse=True
    )
    return {"leaderboard": ranked}

# global weapon stats for an elo range
# both mode is the bracket meta
# opponent mode is performance against the bracket
# exclude is a comma separated list of names
# no elo params gives the full range for the slider
@app.get("/weapons")
def get_weapons(min_elo: float | None = None, max_elo: float | None = None,
                mode: Literal["both", "opponent"] = "both", exclude: str | None = None):
    base_key = _match_files_key()
    facts = _build_weapon_facts(base_key)

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
    # week window cutoff
    cutoff = None
    if window == "week":
        cutoff = datetime.now(timezone.utc).timestamp() - (7 * 24 * 3600)

    timestamps = []
    # merged list so each game counts once
    # and invalid or excluded matches are left out
    for m in merge_matches():
        ts = m.get("timestamp")
        if not ts:
            continue
        # skip matches older than the cutoff
        if cutoff is not None:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if t < cutoff:
                    continue
            except ValueError:
                continue
        # needs a player at or above min lp
        if min_lp > 0:
            players = m.get("players", [])
            if not any((p.get("leaguePoints") or 0) >= min_lp for p in players):
                continue
        timestamps.append(ts)
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