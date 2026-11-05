import argparse
import glob
import os
import sqlite3
from datetime import datetime, timezone

import db

# copies the database to a backup folder and keeps the newest few copies
# safe while the poller and api are running since sqlite copies a consistent snapshot
# a plain file copy can miss changes still waiting in the wal file
# run daily from task scheduler with the folder set to one that syncs off the pc

BACKUP_DIR = os.environ.get("BLASTATS_BACKUP_DIR") or os.path.join(db.DATA_DIR, "backups")
LOG_FILE = os.path.join(db.DATA_DIR, "backup.log")
KEEP = 7  # backups kept before the oldest are deleted
PREFIX = "blastats-"

# print a timestamped line and append it to the log file
# task scheduler hides the output so the log is where problems show up
def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# copy the database into folder and return the new file
def backup(folder):
    os.makedirs(folder, exist_ok=True)
    name = f"{PREFIX}{datetime.now().strftime('%Y-%m-%d-%H%M')}.db"
    path = os.path.join(folder, name)
    # written under a temp name so a half made copy never looks like a backup
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    # sqlite would quietly make an empty database if it was missing
    if not os.path.exists(db.DB_FILE):
        raise RuntimeError(f"no database at {db.DB_FILE}")
    src = sqlite3.connect(db.DB_FILE, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        # one step so a write from the poller part way through cant restart the copy
        # in wal mode the poller and api carry on writing and reading while it runs
        src.backup(dst)
        # make sure the copy opens and isnt damaged before trusting it
        result = dst.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup failed its check: {result}")
        matches = dst.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    except BaseException:
        src.close()
        dst.close()
        os.remove(tmp)
        raise
    src.close()
    dst.close()
    os.replace(tmp, path)
    log(f"backed up {matches} matches to {path} ({os.path.getsize(path) / 1e6:.1f} MB)")
    return path

# delete all but the newest keep backups in folder
# names start with the date so sorting them sorts by age
def prune(folder, keep):
    old = sorted(glob.glob(os.path.join(folder, f"{PREFIX}*.db")))[:-keep]
    for path in old:
        os.remove(path)
        log(f"deleted old backup {os.path.basename(path)}")

# run by task scheduler once a day or by hand before anything risky
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="back up the blastats database")
    parser.add_argument("--dest", default=BACKUP_DIR, help="folder to save backups in")
    parser.add_argument("--keep", type=int, default=KEEP, help="how many backups to keep")
    args = parser.parse_args()
    if args.keep < 1:
        raise SystemExit("--keep needs to be at least 1")
    try:
        backup(args.dest)
        prune(args.dest, args.keep)
    except Exception as e:
        log(f"backup failed: {e}")
        raise SystemExit(1)
