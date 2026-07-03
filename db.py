"""
db.py — SQLite database helpers for Meta Ads Gmail Monitor.

Schema
------
charges  : stores every parsed Facebook charge (deduped by message_id)
state    : key-value store for last_message_id and EWMA state

The DB file lives at ./data/charges.db, which is excluded from git via
.gitignore and persisted between GitHub Actions runs via actions/cache.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "charges.db")

# ── Schema ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT    NOT NULL UNIQUE,
    timestamp   TEXT    NOT NULL,   -- ISO-8601 UTC, e.g. "2026-07-04T10:23:00+00:00"
    amount      REAL    NOT NULL,
    merchant    TEXT    NOT NULL,
    card_name   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ── Public API ─────────────────────────────────────────────────────────────

def get_db_path() -> str:
    """Return the absolute path to the database file."""
    return DB_PATH


def init_db() -> sqlite3.Connection:
    """
    Create (or open) the database, apply the schema, and return a connection.
    The data/ directory is created automatically if it does not exist.
    """
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    logger.info("Database ready at %s", DB_PATH)
    return conn


def insert_charge(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    timestamp: datetime,
    amount: float,
    merchant: str,
    card_name: str,
) -> bool:
    """
    Insert a charge record.  Returns True if inserted, False if it already
    exists (duplicate message_id → silently skipped).
    """
    ts_str = timestamp.astimezone(timezone.utc).isoformat()
    try:
        conn.execute(
            """
            INSERT INTO charges (message_id, timestamp, amount, merchant, card_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, ts_str, amount, merchant, card_name),
        )
        conn.commit()
        logger.debug("Inserted charge: message_id=%s amount=%.2f", message_id, amount)
        return True
    except sqlite3.IntegrityError:
        logger.debug("Duplicate message_id=%s — already processed, skipping.", message_id)
        return False


def get_charges_since(
    conn: sqlite3.Connection, since: datetime
) -> list[sqlite3.Row]:
    """
    Return all charges with timestamp >= *since* (UTC), ordered oldest-first.
    """
    since_str = since.astimezone(timezone.utc).isoformat()
    cur = conn.execute(
        "SELECT * FROM charges WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since_str,),
    )
    return cur.fetchall()


def get_all_charges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all charges ordered oldest-first."""
    cur = conn.execute("SELECT * FROM charges ORDER BY timestamp ASC")
    return cur.fetchall()


def get_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve a value from the key-value state table."""
    cur = conn.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a value in the key-value state table."""
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
