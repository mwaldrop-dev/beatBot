"""
SQLite database for tracking which newsletters have been processed.
"""

import sqlite3
import logging
from datetime import datetime
from src.config import DB_PATH
import os

logger = logging.getLogger(__name__)


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create tables if they don't exist."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS newsletters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_id    TEXT UNIQUE NOT NULL,
                subject     TEXT,
                date_str    TEXT,
                url         TEXT,
                fetched_at  TEXT,
                announced   INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS poll_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        con.commit()
    logger.info("Database initialized")


def is_processed(gmail_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM newsletters WHERE gmail_id = ?", (gmail_id,)
        ).fetchone()
    return row is not None


def mark_processed(gmail_id: str, subject: str, date_str: str, url: str):
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO newsletters (gmail_id, subject, date_str, url, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            (gmail_id, subject, date_str, url, datetime.utcnow().isoformat()),
        )
        con.commit()


def mark_announced(gmail_id: str):
    with _conn() as con:
        con.execute(
            "UPDATE newsletters SET announced = 1 WHERE gmail_id = ?", (gmail_id,)
        )
        con.commit()


def get_last_poll_time() -> datetime | None:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM poll_state WHERE key = 'last_poll_at'"
        ).fetchone()
    if row:
        return datetime.fromisoformat(row[0])
    return None


def set_last_poll_time(dt: datetime):
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO poll_state (key, value) VALUES ('last_poll_at', ?)",
            (dt.isoformat(),),
        )
        con.commit()
