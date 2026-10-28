import argparse
import json
import os
from collections import Counter, defaultdict

import db
import store
from api import WEAPON_NAMES
from store import PAIR_WINDOW, Skip, is_valid_match, low, pair_key, to_float, to_int

# json files in the data folder
MATCHES_FILE = "matches.json"
ALL_MATCHES_FILE = "all_matches.json"
PLAYERS_FILE = "players.json"
LEADERBOARD_FILE = "leaderboard.json"
CORRECTIONS_FILE = "corrections.json"
TOURNAMENTS_FILE = "tournaments.json"

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

# players
# load players and their stats
# returns every player id for linking ladder entries
def import_players(conn, players, now):
    rows, stats = [], []
    for key, p in players.items():
        name = p.get("displayName")
        if not name:
            skipped["player with no name"] += 1
            continue
        first = db.norm_ts(p.get("firstSeen")) or db.norm_ts(p.get("lastSeen")) or now
        last = db.norm_ts(p.get("lastSeen")) or first
        rows.append((key, name, p.get("source") or "unknown", first, last,
                     db.norm_ts(p.get("accountCreated")), db.norm_ts(p.get("accountCheckedAt")),
                     db.norm_ts(p.get("statsLastUpdated")), db.norm_ts(p.get("statsBackfilledAt"))))
        for stat, value in (p.get("stats") or {}).items():
            value = to_int(value)
            if value is not None:
                stats.append((key, stat, value))

    conn.executemany("INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO player_stats VALUES (?, ?, ?)", stats)
    return {r[0] for r in rows}

# seasons
# current and previous season ladders
def import_leaderboard(conn, lb, player_ids, now):
    updated = db.norm_ts(lb.get("lastUpdated")) or now
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
# every known weapon so loadouts can be stored under display names
def import_weapons(conn):
    conn.executemany("INSERT INTO weapons VALUES (?, ?)",
                     [(display, internal) for internal, display in WEAPON_NAMES.items()])

# match pairing
# same rules as _build_base_merged in the api
# returns spectated and announcement pairs where either side can be none

def pair_matches(mod_matches, announcements):
    # group announcements by player pair
    ann_by_pair = defaultdict(list)
    for ann in announcements:
        t = db.parse_ts(ann.get("timestamp"))
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
        t = db.parse_ts(m.get("timestamp"))
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
def import_tournaments(conn, tournaments):
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
                         (tid, display, store.player_id(conn, display), place, to_float(p.get("prizeMoney")) or 0))

# run everything inside one transaction
def import_all(conn, data_dir):
    path = lambda f: os.path.join(data_dir, f)
    now = db.now_iso()

    import_weapons(conn)
    player_ids = import_players(conn, load_json(path(PLAYERS_FILE), {}), now)
    import_leaderboard(conn, load_json(path(LEADERBOARD_FILE), {}), player_ids, now)

    # oldest first so match ids follow time
    pairs = pair_matches(load_json(path(MATCHES_FILE), []), load_json(path(ALL_MATCHES_FILE), []))
    pairs.sort(key=lambda pair: db.parse_ts((pair[1] or pair[0]).get("timestamp")))
    for mod, ann in pairs:
        try:
            store.insert_match(conn, mod, ann, renumber=True)
        except Skip as e:
            skipped[str(e)] += 1

    import_corrections(conn, load_json(path(CORRECTIONS_FILE), {}), now)
    import_tournaments(conn, load_json(path(TOURNAMENTS_FILE), []))

# build into a temp file and swap it in at the end
# a failed import never leaves a half filled database behind
def run(data_dir, out, replace):
    skipped.clear()
    store.notes.clear()
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
    if store.notes:
        print("kept with notes")
        for reason, n in store.notes.most_common():
            print(f"  {reason:<50} {n}")

# run by hand once to move the json data into the database
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="load the json files into a new blastats database")
    parser.add_argument("--data", default=db.DATA_DIR, help="folder with the json files")
    parser.add_argument("--out", default=db.DB_FILE, help="database file to create")
    parser.add_argument("--replace", action="store_true", help="overwrite the database if it exists")
    args = parser.parse_args()
    run(args.data, args.out, args.replace)
