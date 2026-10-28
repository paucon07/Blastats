import re
from collections import Counter
from datetime import timedelta

import db

# saving matches into the database
# shared by the one off json import and the poller so both store a match the same way

# a spectated match and an announcement this many seconds apart are the same game
PAIR_WINDOW = 180

# small problems that dont stop a match being saved
notes = Counter()

# raised when a match cant be saved with the reason as the message
class Skip(Exception):
    pass

# helpers
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

# lowercase names of both players as a set
def pair_key(m):
    return frozenset(low(p.get("displayName")) for p in m.get("players", []))

# check a spectated match is a real best of 3 result like the old api did
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

# player id for a name or none
# real ids win over legacy name ids then whoever was seen most recently
def player_id(conn, name):
    row = conn.execute("SELECT player_id FROM players WHERE display_name = ? COLLATE NOCASE "
                       "ORDER BY player_id LIKE 'name:%', last_seen DESC LIMIT 1", (name,)).fetchone()
    return row[0] if row else None

# display name for a weapon id or name
# unknown ones are added to the weapons table as they are
def weapon_name(conn, name):
    row = conn.execute("SELECT name FROM weapons WHERE name = ? COLLATE NOCASE OR internal_id = ? COLLATE NOCASE",
                       (name, name)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT INTO weapons (name) VALUES (?)", (name,))
    return name

# saving
# insert one match and everything under it and return its id
# mod is the spectated copy and ann the announcement and either can be none
# renumber keeps announcements that reuse an id instead of skipping them
def insert_match(conn, mod, ann, renumber=False):
    base = mod or ann
    players = base.get("players", [])
    names = [low(p.get("displayName")) for p in players]
    if len(players) != 2 or not all(names):
        raise Skip("match without two players")
    network_ids = [to_int(p.get("networkId")) for p in players]

    # winners by slot from the round winners list
    round_slots = [find_slot(r, names, network_ids) for r in base.get("roundWinners") or []]
    winner_slot = find_slot(base.get("winner"), names, network_ids)
    if None in round_slots or winner_slot is None:
        raise Skip("round winner not one of the players")
    # old poller rows picked up rounds from other games
    if len(round_slots) not in (2, 3):
        raise Skip("more than 3 rounds or fewer than 2")
    # two headsets on one account so names cant tell the sides apart
    # slot 0 wins and the round order is guessed like the poller does
    if names[0] == names[1]:
        winner_slot = 0
        round_slots = [0, 0] if len(round_slots) == 2 else [0, 1, 0]

    ann_id = ann.get("match_id") if ann else None
    spec_id = mod.get("match_id") if mod else None
    taken = lambda col, v: conn.execute(f"SELECT 1 FROM matches WHERE {col} = ?", (v,)).fetchone()
    if ann_id and taken("announcement_id", ann_id):
        if not renumber:
            raise Skip("announcement already saved")
        # hand made rows sometimes reused an id so they get a numbered copy of it
        n = 2
        while taken("announcement_id", f"{ann_id}#{n}"):
            n += 1
        ann_id = f"{ann_id}#{n}"
        notes["duplicate announcement id kept with a number added"] += 1
    if spec_id and taken("spectate_id", spec_id):
        raise Skip("spectated match already saved")

    # announcement time when there is one like the api
    started = db.norm_ts((ann or mod).get("timestamp"))
    if started is None:
        raise Skip("match with a bad timestamp")

    cur = conn.execute(
        "INSERT INTO matches (announcement_id, spectate_id, started_at, ended_at, spectated, "
        "region, end_reason, winner_slot, round_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ann_id, spec_id, started, db.norm_ts(mod.get("matchEndTime")) if mod else None,
         1 if mod else 0, mod.get("region") if mod else None,
         mod.get("endReason") if mod else None, winner_slot, len(round_slots)))
    match_id = cur.lastrowid

    # elo and rounds start only come from the announcement
    ann_players = {low(p.get("displayName")): p for p in (ann.get("players", []) if ann else [])}
    for slot, p in enumerate(players):
        a = ann_players.get(names[slot]) or p
        conn.execute(
            "INSERT INTO match_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, slot, player_id(conn, names[slot]), p.get("displayName"),
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
        notes["round detail that doesnt match the round count"] += 1
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
                notes["damage event missing a field or receiver"] += 1
                continue
            conn.execute("INSERT INTO round_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (match_id, number, seq, t, e.get("weapon") or "", receiver, dmg))
            seq += 1
    return match_id

# live pairing
# the poller and the mod each save their copy of a game whichever finishes first
# so each side looks for the other already in the database

# matches for the same two players within the pair window closest first
def _nearby(conn, names, t, spectated):
    window = timedelta(seconds=PAIR_WINDOW)
    rows = conn.execute("SELECT id, started_at, announcement_id FROM matches "
                        "WHERE spectated = ? AND started_at BETWEEN ? AND ?",
                        (spectated, db.iso(t - window), db.iso(t + window))).fetchall()
    out = []
    for r in rows:
        players = {low(p[0]) for p in conn.execute("SELECT display_name FROM match_players WHERE match_id = ?", (r["id"],))}
        dt = abs((db.parse_ts(r["started_at"]) - t).total_seconds())
        if players == set(names) and dt < PAIR_WINDOW:
            out.append((dt, r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out]

# save a finished poller match
# joins a spectated copy already saved or is stored on its own
def add_announcement(conn, ann):
    t = db.parse_ts(ann.get("timestamp"))
    if t is None:
        raise Skip("announcement with a bad timestamp")
    if ann.get("match_id") and conn.execute("SELECT 1 FROM matches WHERE announcement_id = ?",
                                            (ann["match_id"],)).fetchone():
        raise Skip("announcement already saved")

    nearby = _nearby(conn, pair_key(ann), t, spectated=1)
    unpaired = [r for r in nearby if r["announcement_id"] is None]
    if unpaired:
        # spectated copy is already saved so it takes the announcement id time and elo
        match_id = unpaired[0]["id"]
        conn.execute("UPDATE matches SET announcement_id = ?, started_at = ? WHERE id = ?",
                     (ann.get("match_id"), db.iso(t), match_id))
        for p in ann.get("players", []):
            conn.execute("UPDATE match_players SET elo = ?, rounds_start = ? "
                         "WHERE match_id = ? AND display_name = ? COLLATE NOCASE",
                         (to_int(p.get("elo")), to_int(p.get("roundsStart")), match_id, p.get("displayName")))
        return match_id
    # a spectated match close by already has its announcement
    if nearby:
        raise Skip("announcement duplicating a spectated match")
    return insert_match(conn, None, ann)

# save a spectated match from the mod
# takes over a poller copy already saved or is stored on its own
def add_spectated(conn, mod):
    if not is_valid_match(mod):
        raise Skip("spectated match not a valid best of 3")
    t = db.parse_ts(mod.get("timestamp"))
    if t is None:
        raise Skip("spectated match with a bad timestamp")
    # older mod rows have no match id so the end time picks out ones already saved
    ended = db.norm_ts(mod.get("matchEndTime"))
    if ended and conn.execute("SELECT 1 FROM matches WHERE spectated = 1 AND ended_at = ?", (ended,)).fetchone():
        raise Skip("spectated match already saved")

    ann = None
    nearby = _nearby(conn, pair_key(mod), t, spectated=0)
    if nearby:
        # rebuild the announcement from its row then replace the row with the joined match
        row = nearby[0]
        m = conn.execute("SELECT announcement_id, started_at FROM matches WHERE id = ?", (row["id"],)).fetchone()
        ann = {"match_id": m["announcement_id"], "timestamp": m["started_at"],
               "players": [{"displayName": p["display_name"], "elo": p["elo"], "roundsStart": p["rounds_start"]}
                           for p in conn.execute("SELECT * FROM match_players WHERE match_id = ?", (row["id"],))]}
        conn.execute("DELETE FROM matches WHERE id = ?", (row["id"],))
    return insert_match(conn, mod, ann)
