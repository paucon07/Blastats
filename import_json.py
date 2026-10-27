import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import db
from api import WEAPON_NAMES

# json files in the data folder
MATCHES_FILE = "matches.json"
ALL_MATCHES_FILE = "all_matches.json"
PLAYERS_FILE = "players.json"
LEADERBOARD_FILE = "leaderboard.json"
CORRECTIONS_FILE = "corrections.json"
TOURNAMENTS_FILE = "tournaments.json"

# a spectated match and an announcement this many seconds apart are the same game
PAIR_WINDOW = 180

# reasons rows were left out
# printed at the end so nothing goes missing silently
skipped = Counter()

# helpers
# read a json file or return default if missing or broken
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return default
    return default

# iso timestamp to utc datetime or none
def parse_ts(s):
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    # the mod writes 7 digit fractions and older pythons only read 3 or 6
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    s = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # no timezone so assume utc like rectify
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# utc datetime to the one text format used in the database
# same length every time so timestamps sort correctly as text
def iso(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

# timestamp text to database format or none
def norm_ts(s):
    dt = parse_ts(s)
    return iso(dt) if dt else None

# value as an int or none
def to_int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None

# value as a float or none
def to_float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None

# lowercase name used for every name comparison like the api
def low(name):
    return (name or "").lower()

# players
# load players and their stats
# returns every player id plus lowercase name to player id for linking matches
def import_players(conn, players, now):
    rows, stats = [], []
    for key, p in players.items():
        name = p.get("displayName")
        if not name:
            skipped["player with no name"] += 1
            continue
        first = norm_ts(p.get("firstSeen")) or norm_ts(p.get("lastSeen")) or now
        last = norm_ts(p.get("lastSeen")) or first
        rows.append((key, name, p.get("source") or "unknown", first, last,
                     norm_ts(p.get("accountCreated")), norm_ts(p.get("accountCheckedAt")),
                     norm_ts(p.get("statsLastUpdated")), norm_ts(p.get("statsBackfilledAt"))))
        for stat, value in (p.get("stats") or {}).items():
            value = to_int(value)
            if value is not None:
                stats.append((key, stat, value))

    conn.executemany("INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO player_stats VALUES (?, ?, ?)", stats)

    # real ids win over legacy name ids then the most recently seen wins
    # so a reused name links to the account using it now
    by_name = {}
    for key, name, _, _, last, *_ in sorted(rows, key=lambda r: (not r[0].startswith("name:"), r[4])):
        by_name[low(name)] = key
    return {r[0] for r in rows}, by_name

# seasons
# current and previous season ladders
def import_leaderboard(conn, lb, player_ids, now):
    updated = norm_ts(lb.get("lastUpdated")) or now
    seen_versions = set()
    for status, key in (("current", "currentSeason"), ("previous", "previousSeason")):
        season = lb.get(key) or {}
        if season.get("version") is None:
            continue
        version = str(season["version"])
        # same version twice would break the primary key
        if version in seen_versions:
            skipped["previous season same as current"] += 1
            continue
        seen_versions.add(version)
        conn.execute("INSERT INTO seasons VALUES (?, ?, ?, ?)", (version, updated, status, updated))

        positions = set()
        for e in season.get("leaderboard") or []:
            # server ranks start at 0
            position = (to_int(e.get("position")) or 0) + 1
            if position in positions:
                skipped["duplicate ladder position"] += 1
                continue
            positions.add(position)
            pid = e.get("playFabMasterPlayerId")
            conn.execute("INSERT INTO leaderboard_entries VALUES (?, ?, ?, ?, ?, ?, ?)", (
                version, position, pid if pid in player_ids else None,
                e.get("displayName") or "Unknown", to_int(e.get("value")) or 0,
                to_int(e.get("secondaryValue")), e.get("formattedValue")))

# weapons
# any id or display name to display name ignoring case
WEAPON_CANON = {}
for _internal, _display in WEAPON_NAMES.items():
    WEAPON_CANON[_internal.lower()] = _display
    WEAPON_CANON[_display.lower()] = _display

def import_weapons(conn):
    conn.executemany("INSERT INTO weapons VALUES (?, ?)",
                     [(display, internal) for internal, display in WEAPON_NAMES.items()])

# display name for a weapon adding any unknown ones to the weapons table
def weapon_name(conn, name):
    canon = WEAPON_CANON.get(name.lower())
    if canon:
        return canon
    conn.execute("INSERT OR IGNORE INTO weapons (name) VALUES (?)", (name,))
    WEAPON_CANON[name.lower()] = name
    return name

# match pairing
# same rules as _build_base_merged in the api
# returns spectated and announcement pairs where either side can be none

# lowercase names of both players as a set
def pair_key(m):
    return frozenset(low(p.get("displayName")) for p in m.get("players", []))

# check a match is a real best of 3 result like is_valid_match
def is_valid_match(m):
    players = m.get("players", [])
    winner = m.get("winner", "")
    round_winners = m.get("roundWinners", [])
    if len(players) != 2 or not winner:
        return False
    n = len(round_winners)
    if n < 2 or n > 3:
        return False
    winner_rounds = sum(1 for r in round_winners if low(r) == low(winner))
    return winner_rounds == 2 and (n - winner_rounds) <= 1

def pair_matches(mod_matches, announcements):
    # group announcements by player pair
    ann_by_pair = defaultdict(list)
    for ann in announcements:
        t = parse_ts(ann.get("timestamp"))
        if t is None:
            skipped["announcement with a bad timestamp"] += 1
            continue
        ann_by_pair[pair_key(ann)].append((t, ann))

    # valid spectated matches in the same order as the api
    mods = []
    for m in mod_matches:
        if is_valid_match(m):
            mods.append(m)
        else:
            skipped["spectated match not a valid best of 3"] += 1
    mods.sort(key=lambda m: m.get("timestamp", ""))

    pairs = []
    times_by_pair = defaultdict(list)
    for m in mods:
        t = parse_ts(m.get("timestamp"))
        if t is None:
            skipped["spectated match with a bad timestamp"] += 1
            continue
        key = pair_key(m)
        # closest announcement for the same pair inside the window
        best_i, best_dt = None, float("inf")
        for i, (ann_t, _) in enumerate(ann_by_pair[key]):
            dt = abs((t - ann_t).total_seconds())
            if dt < PAIR_WINDOW and dt < best_dt:
                best_i, best_dt = i, dt
        if best_i is not None:
            ann_t, ann = ann_by_pair[key].pop(best_i)
            pairs.append((m, ann))
            times_by_pair[key].append(ann_t)
        else:
            pairs.append((m, None))
            times_by_pair[key].append(t)

    # leftover announcements unless a spectated match is close by
    for key, entries in ann_by_pair.items():
        for ann_t, ann in entries:
            if any(abs((ann_t - t).total_seconds()) < PAIR_WINDOW for t in times_by_pair[key]):
                skipped["announcement duplicating a spectated match"] += 1
                continue
            pairs.append((None, ann))
    return pairs

# matches
# slot of a player name or actor id or none
def find_slot(name, names, network_ids):
    name = low(name)
    if name in names:
        return names.index(name)
    # the mod writes actor and the network id when it couldnt resolve a name
    m = re.fullmatch(r"actor_(\d+)", name)
    if m and int(m.group(1)) in network_ids:
        return network_ids.index(int(m.group(1)))
    return None

# insert one match and everything under it
# returns the new match id or none if it was left out
def import_match(conn, mod, ann, player_ids, seen_ids):
    base = mod or ann
    players = base.get("players", [])
    names = [low(p.get("displayName")) for p in players]
    if len(players) != 2 or not all(names) or names[0] == names[1]:
        skipped["match without two different players"] += 1
        return None
    network_ids = [to_int(p.get("networkId")) for p in players]

    # winners by slot from the round winners list
    round_slots = [find_slot(r, names, network_ids) for r in base.get("roundWinners") or []]
    winner_slot = find_slot(base.get("winner"), names, network_ids)
    if None in round_slots or winner_slot is None:
        skipped["round winner not one of the players"] += 1
        return None
    if len(round_slots) not in (2, 3) or round_slots.count(winner_slot) != 2:
        skipped["match not a valid best of 3"] += 1
        return None

    ann_id = ann.get("match_id") if ann else None
    spec_id = mod.get("match_id") if mod else None
    if ann_id and ann_id in seen_ids["ann"]:
        skipped["duplicate announcement id"] += 1
        return None
    if spec_id and spec_id in seen_ids["spec"]:
        skipped["duplicate spectated match id"] += 1
        return None

    # announcement time when there is one like the api
    started = norm_ts((ann or mod).get("timestamp"))
    if started is None:
        skipped["match with a bad timestamp"] += 1
        return None
    if ann_id:
        seen_ids["ann"].add(ann_id)
    if spec_id:
        seen_ids["spec"].add(spec_id)

    cur = conn.execute(
        "INSERT INTO matches (announcement_id, spectate_id, started_at, ended_at, spectated, "
        "region, end_reason, winner_slot, round_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ann_id, spec_id, started, norm_ts(mod.get("matchEndTime")) if mod else None,
         1 if mod else 0, mod.get("region") if mod else None,
         mod.get("endReason") if mod else None, winner_slot, len(round_slots)))
    match_id = cur.lastrowid

    # elo and rounds start only come from the announcement
    ann_players = {low(p.get("displayName")): p for p in (ann.get("players", []) if ann else [])}
    for slot, p in enumerate(players):
        a = ann_players.get(names[slot]) or p
        conn.execute(
            "INSERT INTO match_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, slot, player_ids.get(names[slot]), p.get("displayName"),
             to_int(p.get("leaguePoints")), to_int(a.get("elo")),
             to_int(a.get("roundsStart")), network_ids[slot],
             to_int(p.get("characterId")), p.get("character"), p.get("characterName"),
             to_int(p.get("ping"))))
        # scoreboard weapons in the order they were shown
        for position, w in enumerate(p.get("weapons") or []):
            if not w.get("name"):
                continue
            conn.execute("INSERT INTO match_loadouts VALUES (?, ?, ?, ?, ?)",
                         (match_id, slot, position, weapon_name(conn, w["name"]), w.get("attachment") or None))

    # round detail only lines up when the mod has one entry per round
    details = (mod.get("rounds") or []) if mod else []
    if details and len(details) != len(round_slots):
        skipped["round detail that doesnt match the round count"] += 1
        details = []

    for i, winner in enumerate(round_slots):
        number = i + 1
        r = details[i] if details else {}
        duration = to_float(r.get("durationSeconds"))
        forfeit = find_slot(f"actor_{r.get('forfeitPlayerId')}", names, network_ids) if r.get("endedByForfeit") else None
        conn.execute("INSERT INTO rounds VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (match_id, number, winner, duration if duration and duration > 0 else None,
                      forfeit, to_int(r.get("p1Health")), to_int(r.get("p2Health"))))

        # totals per player keyed by network id then type then source
        for nid, by_type in (r.get("stats") or {}).items():
            receiver = find_slot(f"actor_{nid}", names, network_ids)
            if receiver is None or not isinstance(by_type, dict):
                continue
            for stat_type, sources in by_type.items():
                for source, value in (sources or {}).items():
                    value = to_float(value)
                    if value is not None:
                        conn.execute("INSERT INTO round_stats VALUES (?, ?, ?, ?, ?, ?)",
                                     (match_id, number, receiver, stat_type, source, value))

        # per hit timeline in the order logged
        seq = 0
        for e in r.get("events") or []:
            receiver = find_slot(f"actor_{e.get('receiver')}", names, network_ids)
            t, dmg = to_float(e.get("t")), to_float(e.get("dmg"))
            if receiver is None or t is None or dmg is None:
                skipped["damage event missing a field or receiver"] += 1
                continue
            conn.execute("INSERT INTO round_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (match_id, number, seq, t, e.get("weapon") or "", receiver, dmg))
            seq += 1
    return match_id

# corrections
# keyed by announcement id and only ever applied to unspectated matches
def import_corrections(conn, corrections, now):
    for key, c in corrections.items():
        row = conn.execute("SELECT id, winner_slot FROM matches WHERE announcement_id = ? AND spectated = 0",
                           (key,)).fetchone()
        if row is None:
            skipped["correction with no unspectated match"] += 1
            continue
        exclude = 1 if c.get("exclude") else 0
        if not exclude:
            names = [low(r["display_name"]) for r in conn.execute(
                "SELECT display_name FROM match_players WHERE match_id = ? ORDER BY slot", (row["id"],))]
            new = low(c.get("winner"))
            # only a flip to the other player changes anything
            if new not in names or names.index(new) == row["winner_slot"]:
                skipped["correction that doesnt change the winner"] += 1
                continue
        moves = c.get("eloMoves") or [None, None]
        conn.execute("INSERT INTO match_corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            row["id"], "manual" if c.get("source") == "manual" else "auto", exclude,
            c.get("rule"), to_int(moves[0]), to_int(moves[1] if len(moves) > 1 else None),
            c.get("note"), now))

# tournaments
def import_tournaments(conn, tournaments, player_ids):
    for t in tournaments:
        name = t.get("name")
        if not name or conn.execute("SELECT 1 FROM tournaments WHERE name = ?", (name,)).fetchone():
            skipped["tournament with no name or a duplicate name"] += 1
            continue
        # keep the existing id when there is a usable one
        tid = to_int(t.get("id"))
        if tid is not None and conn.execute("SELECT 1 FROM tournaments WHERE id = ?", (tid,)).fetchone():
            tid = None
        tid = conn.execute("INSERT INTO tournaments (id, name) VALUES (?, ?)", (tid, name)).lastrowid
        names = set()
        for p in t.get("placement") or []:
            display = p.get("displayName")
            place = to_int(p.get("place"))
            if not display or place is None or place < 1 or low(display) in names:
                skipped["placement missing a name or place or duplicated"] += 1
                continue
            names.add(low(display))
            conn.execute("INSERT INTO tournament_placements VALUES (?, ?, ?, ?, ?)",
                         (tid, display, player_ids.get(low(display)), place, to_float(p.get("prizeMoney")) or 0))

# run everything inside one transaction
def import_all(conn, data_dir):
    path = lambda f: os.path.join(data_dir, f)
    now = iso(datetime.now(timezone.utc))

    import_weapons(conn)
    all_ids, player_ids = import_players(conn, load_json(path(PLAYERS_FILE), {}), now)
    import_leaderboard(conn, load_json(path(LEADERBOARD_FILE), {}), all_ids, now)

    # oldest first so match ids follow time
    pairs = pair_matches(load_json(path(MATCHES_FILE), []), load_json(path(ALL_MATCHES_FILE), []))
    pairs.sort(key=lambda pair: parse_ts((pair[1] or pair[0]).get("timestamp")))
    seen_ids = {"ann": set(), "spec": set()}
    for mod, ann in pairs:
        import_match(conn, mod, ann, player_ids, seen_ids)

    import_corrections(conn, load_json(path(CORRECTIONS_FILE), {}), now)
    import_tournaments(conn, load_json(path(TOURNAMENTS_FILE), []), player_ids)

# build into a temp file and swap it in at the end
# a failed import never leaves a half filled database behind
def run(data_dir, out, replace):
    skipped.clear()
    if os.path.exists(out) and not replace:
        raise SystemExit(f"{out} already exists, rerun with --replace to overwrite it")
    tmp = out + ".import"
    for f in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(f):
            os.remove(f)

    conn = db.connect(tmp)
    try:
        with conn:
            import_all(conn, data_dir)
        # every foreign key should point at a real row
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"{len(broken)} rows point at missing rows, first is {tuple(broken[0])}")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "players", "player_stats", "seasons", "leaderboard_entries", "weapons", "matches",
            "match_players", "match_loadouts", "rounds", "round_stats", "round_events",
            "match_corrections", "tournaments", "tournament_placements")}
        conn.close()
    except BaseException:
        conn.close()
        for f in (tmp, tmp + "-wal", tmp + "-shm"):
            if os.path.exists(f):
                os.remove(f)
        raise

    # old wal files would be replayed onto the new database so they go first
    # fails on windows if the api or poller still has the database open
    for f in (out + "-wal", out + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    os.replace(tmp, out)

    print(f"imported into {out}")
    for table, n in counts.items():
        print(f"  {table:<22} {n}")
    if skipped:
        print("left out")
        for reason, n in skipped.most_common():
            print(f"  {reason:<50} {n}")

# run by hand once to move the json data into the database
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="load the json files into a new blastats database")
    parser.add_argument("--data", default=db.DATA_DIR, help="folder with the json files")
    parser.add_argument("--out", default=db.DB_FILE, help="database file to create")
    parser.add_argument("--replace", action="store_true", help="overwrite the database if it exists")
    args = parser.parse_args()
    run(args.data, args.out, args.replace)
