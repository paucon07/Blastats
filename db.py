import os
import sqlite3

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

# run by hand to create the database
if __name__ == "__main__":
    conn = connect()
    tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    conn.close()
    print(f"{DB_FILE} ready with {tables} tables")
