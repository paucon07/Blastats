import argparse
import json

import db
import store

# add or update tournaments in the database from a json file
# the file holds one tournament or a list of them in the same shape tournaments json used
# a tournament with the same name has its placements replaced so rerunning a file is safe

# check one tournament and return its name and placement rows
# raises with a message pointing at the problem
def read_tournament(t):
    name = (t.get("name") or "").strip() if isinstance(t, dict) else ""
    if not name:
        raise ValueError("tournament with no name")
    rows, seen = [], set()
    for p in t.get("placement") or []:
        display = (p.get("displayName") or "").strip()
        place = store.to_int(p.get("place"))
        prize = store.to_float(p.get("prizeMoney")) or 0
        if not display:
            raise ValueError(f"{name}: placement with no displayName")
        if place is None or place < 1:
            raise ValueError(f"{name}: {display} needs a place of 1 or more")
        if prize < 0:
            raise ValueError(f"{name}: {display} has negative prize money")
        if display.lower() in seen:
            raise ValueError(f"{name}: {display} is listed twice")
        seen.add(display.lower())
        rows.append((display, place, prize))
    return name, rows

# write every tournament in one transaction
# nothing is saved if any of them has a problem
def save(conn, tournaments):
    checked = [read_tournament(t) for t in tournaments]
    with conn:
        for name, rows in checked:
            row = conn.execute("SELECT id FROM tournaments WHERE name = ?", (name,)).fetchone()
            tid = row[0] if row else conn.execute("INSERT INTO tournaments (name) VALUES (?)", (name,)).lastrowid
            conn.execute("DELETE FROM tournament_placements WHERE tournament_id = ?", (tid,))
            # linked to a tracked player when the name matches one
            conn.executemany("INSERT INTO tournament_placements VALUES (?, ?, ?, ?, ?)",
                             [(tid, display, store.player_id(conn, display), place, prize)
                              for display, place, prize in rows])
            print(f"{'updated' if row else 'added'} {name} with {len(rows)} placements")

# delete a tournament and its placements by name
def remove(conn, name):
    with conn:
        n = conn.execute("DELETE FROM tournaments WHERE name = ?", (name,)).rowcount
    print(f"removed {name}" if n else f"no tournament called {name}")

# every tournament with its placement count and total prize money
def show(conn):
    for r in conn.execute("SELECT t.name, COUNT(p.place), COALESCE(SUM(p.prize_money), 0) FROM tournaments t "
                          "LEFT JOIN tournament_placements p ON p.tournament_id = t.id GROUP BY t.id ORDER BY t.id"):
        print(f"{r[0]:<40} {r[1]:>3} placements  ${r[2]:,.2f}")

# run by hand whenever a tournament is added or changed
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="add or update blastats tournaments")
    parser.add_argument("file", nargs="?", help="json file with one tournament or a list of them")
    parser.add_argument("--remove", metavar="NAME", help="delete the tournament with this name")
    parser.add_argument("--list", action="store_true", help="show every tournament")
    args = parser.parse_args()

    conn = db.connect()
    try:
        if args.file:
            try:
                with open(args.file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise SystemExit(f"couldnt read {args.file}: {e}")
            save(conn, data if isinstance(data, list) else [data])
        if args.remove:
            remove(conn, args.remove)
        if args.list or not (args.file or args.remove):
            show(conn)
    except ValueError as e:
        # bad input gets a short message instead of a traceback
        raise SystemExit(f"nothing saved: {e}")
    finally:
        conn.close()
