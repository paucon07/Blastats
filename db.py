import os
import re
import sqlite3
from datetime import datetime, timezone

# file paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_FILE = os.path.join(DATA_DIR, "blastats.db")
SCHEMA_FILE = os.path.join(BASE_DIR, "schema.sql")

# keep in sync with user_version at the bottom of schema.sql
SCHEMA_VERSION = 1

# seconds to wait for the poller to finish a write before giving up
BUSY_TIMEOUT = 10

# open the database with the settings every script needs
# creates it from the schema on first use
def connect(path=DB_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT)
    # rows can be read by column name as well as position
    db.row_factory = sqlite3.Row
    # off by default and only lasts for this connection
    db.execute("PRAGMA foreign_keys = ON")
    init_db(db)
    return db

# schema version stored in the database file
def schema_version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]

# build the tables on a new database
# an older database needs migrating so it isnt touched
def init_db(db):
    # version 0 means nothing has been created yet
    if schema_version(db) == 0:
        with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
            db.executescript(f.read()) # executescript used over execute as it can run many sql statements
    version = schema_version(db)
    if version != SCHEMA_VERSION:
        db.close()
        raise RuntimeError(f"database is on schema version {version} but the code expects {SCHEMA_VERSION}")

# timestamps
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

# current time in database format
def now_iso():
    return iso(datetime.now(timezone.utc))

# run by hand to create the database
if __name__ == "__main__":
    conn = connect()
    tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    conn.close()
    print(f"{DB_FILE} ready with {tables} tables")
