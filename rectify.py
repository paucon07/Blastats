import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

# elo moves this size or smaller are noise
EPS = 2
# min elo gap for a silent upset
MIN_GAP = 200
# no matches from anyone for this long means the tracker was down
DOWNTIME_HOURS = 18

# sorts missing timestamps last
FAR = datetime.max.replace(tzinfo=timezone.utc)


# read a json file or none if missing or broken
def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

# write to a temp file then swap it in
def save_json(path, data):
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

# iso timestamp to utc datetime or none
def parse_ts(s):
    try:
        # strip z so both z and +00:00 forms parse
        dt = datetime.fromisoformat(str(s).rstrip("Z"))
    except (ValueError, TypeError):
        return None
    # no timezone so assume utc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# each player's matches oldest first plus match id to position
def build_timelines(matches):
    per = defaultdict(list)
    for m in matches:
        dt = parse_ts(m.get("timestamp"))
        for p in m.get("players", []):
            nm = (p.get("displayName") or "").lower()
            if nm:
                per[nm].append({"mid": m.get("match_id"), "dt": dt, "elo": p.get("elo"),
                                "rounds": p.get("roundsStart")})
    for tl in per.values():
        tl.sort(key=lambda e: (e["dt"] is None, e["dt"] or FAR))
    pos = {nm: {e["mid"]: i for i, e in enumerate(tl)} for nm, tl in per.items()}
    return per, pos

# gaps where nobody was recorded for downtime hours
def downtime_gaps(matches):
    ts = sorted(t for t in (parse_ts(m.get("timestamp")) for m in matches) if t)
    return [(a, b) for a, b in zip(ts, ts[1:])
            if (b - a).total_seconds() >= DOWNTIME_HOURS * 3600]

# true if downtime falls between t0 and t1
# t1 none means up to now
def in_downtime(t0, t1, gaps):
    return t0 is None or any(a >= t0 and (t1 is None or b <= t1) for a, b in gaps)

# a player's elo after match i or none if it cant be trusted
def elo_after(tl, i, current, gaps):
    this = tl[i]
    if i + 1 < len(tl):
        # next match start elo is this match end elo
        # never look further ahead or other games get mixed in
        nxt = tl[i + 1]
        after, rounds_after, t1 = nxt["elo"], nxt["rounds"], nxt["dt"]
    else:
        # latest match so use current stats if refreshed since
        after, rounds_after, updated = current
        if this["dt"] is None or updated is None or updated <= this["dt"]:
            return None
        t1 = None
    if after is None or in_downtime(this["dt"], t1, gaps):
        return None
    # a player wins at most 2 rounds in a match
    # more than that means an unrecorded game happened in between
    # not tied to the recorded result since that might be the wrong one
    # older matches have no rounds start so skip the check
    if this["rounds"] is not None and rounds_after is not None \
            and not 0 <= rounds_after - this["rounds"] <= 2:
        return None
    return after

# new winner and why if elo says the recorded winner is wrong
def judge(m, timelines, pos, current, gaps):
    players = m.get("players", [])
    winner = (m.get("winner") or "").lower()
    if len(players) != 2 or not winner:
        return None

    # name before elo and elo change for both players
    side = {}
    for p in players:
        nm = (p.get("displayName") or "").lower()
        i = pos.get(nm, {}).get(m.get("match_id"))
        if i is None:
            return None
        before = p.get("elo")
        after = elo_after(timelines[nm], i, current.get(nm, (None, None, None)), gaps)
        if before is None or after is None:
            return None
        side[nm] = (p.get("displayName"), before, int(after - before))

    # winner must be one of two different players
    if winner not in side or len(side) != 2:
        return None
    loser = next(k for k in side if k != winner)
    _, w_before, w_move = side[winner]
    l_name, l_before, l_move = side[loser]

    # winner lost elo and loser gained so the loser won
    if w_move < -EPS and l_move > EPS:
        return l_name, "strong", w_move, l_move
    # big underdog recorded winning but nobody moved so the favourite won
    if abs(w_move) <= EPS and abs(l_move) <= EPS and l_before - w_before >= MIN_GAP:
        return l_name, "silent_upset", w_move, l_move
    return None


# rebuild corrections json from scratch and return the number of flips
def rectify(data_dir):
    matches = load_json(os.path.join(data_dir, "all_matches.json"))
    players = load_json(os.path.join(data_dir, "players.json"))
    # a bad read would wipe every flip so keep the old file
    if not isinstance(matches, list) or not matches or not isinstance(players, dict):
        raise ValueError("match or player data missing or unreadable, corrections left as is")

    timelines, pos = build_timelines(matches)
    gaps = downtime_gaps(matches)

    # lowercase name to current elo rounds and last refresh
    current = {}
    for p in players.values():
        nm = (p.get("displayName") or "").lower()
        if nm:
            st = p.get("stats") or {}
            current[nm] = (st.get("ELO_RATING"), st.get("ROUNDS_WON"),
                           parse_ts(p.get("statsLastUpdated")))

    corrections = {}
    for m in matches:
        result = judge(m, timelines, pos, current, gaps)
        if result and m.get("match_id"):
            new_winner, rule, w_move, l_move = result
            # api only reads winner
            # the rest is there to check flips by eye
            corrections[m["match_id"]] = {"winner": new_winner, "wasRecorded": m.get("winner"),
                                          "rule": rule, "eloMoves": [w_move, l_move]}

    save_json(os.path.join(data_dir, "corrections.json"), corrections)
    return len(corrections)


# run by hand on the data folder next to this file
if __name__ == "__main__":
    print(rectify(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")), "flips")