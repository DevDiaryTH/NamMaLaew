"""SQLite storage shared by the monitor (writer) and the dashboard (reader + settings writer).

All timestamps are ISO-8601 UTC strings, e.g. "2026-09-25T14:03:12+00:00".
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    status               TEXT NOT NULL,          -- normal | warning | critical | unknown (final, after thresholds)
    model_status         TEXT,                   -- status the model itself returned
    level_index          REAL,                   -- 0-100, NULL when unknown
    confidence           REAL,
    description          TEXT,
    distance_to_critical TEXT,
    reason               TEXT,
    composite_path       TEXT,                   -- relative to SNAPSHOT_DIR
    model                TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             REAL,
    capture_ms           INTEGER,
    analysis_ms          INTEGER,
    error                TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS lens_readings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id         INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    label              TEXT NOT NULL,
    snapshot_path      TEXT,                     -- relative to SNAPSHOT_DIR, NULL if capture failed
    ok                 INTEGER NOT NULL,         -- 1 captured, 0 failed
    observation        TEXT,
    water_coverage_pct REAL,                     -- model estimate, % of visible ground covered by water
    brightness         REAL,                     -- 0-255 mean luma, computed locally
    sharpness          REAL,                     -- variance of Laplacian-like edge measure, computed locally
    is_night           INTEGER,                  -- 1 if frame looks like IR/grayscale night mode
    error              TEXT,
    line_position      TEXT                      -- see LINE_POSITIONS; NULL for readings before alert lines existed
);
CREATE INDEX IF NOT EXISTS idx_lens_reading ON lens_readings(reading_id);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,                    -- critical | warning | recovered | failure | heartbeat | test
    message    TEXT NOT NULL,
    delivered  INTEGER NOT NULL,                 -- 1 sent OK, 0 failed / skipped
    error      TEXT,
    reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather (
    hour_ts          TEXT PRIMARY KEY,           -- UTC hour start
    precipitation_mm REAL,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id        INTEGER NOT NULL UNIQUE REFERENCES readings(id) ON DELETE CASCADE,
    ts                TEXT NOT NULL,
    verdict           TEXT NOT NULL,             -- correct | wrong
    true_status       TEXT,                      -- normal | warning | critical
    true_level_index  REAL,
    note              TEXT,
    is_example        INTEGER NOT NULL DEFAULT 0,
    example_dir       TEXT                       -- relative to snapshot_dir(), NULL when not an example
);
CREATE INDEX IF NOT EXISTS idx_feedback_reading ON feedback(reading_id);
"""


# Where the water sits relative to the user-drawn alert lines of a lens (see wlm/lines.py).
LINE_POSITIONS = ("no_lines", "not_visible", "below_warning", "at_or_above_warning", "at_or_above_critical")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lens_readings)")}
    if "line_position" not in cols:
        conn.execute("ALTER TABLE lens_readings ADD COLUMN line_position TEXT")
        conn.commit()
    # feedback table is created by SCHEMA (CREATE IF NOT EXISTS), no column migration needed


def db_path() -> Path:
    return Path(os.getenv("WLM_DB", "data/wlm.db"))


def snapshot_dir() -> Path:
    return Path(os.getenv("WLM_SNAPSHOT_DIR", "snapshots"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    """Use the given connection, or open (and close) a fresh one."""
    if conn is not None:
        yield conn
        return
    own = connect()
    try:
        yield own
    finally:
        own.close()


def _insert(table: str, values: dict, conn=None) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with _conn(conn) as c:
        cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
        c.commit()
        return cur.lastrowid


def insert_reading(values: dict, conn=None) -> int:
    values = {"ts": now_iso(), **values}
    return _insert("readings", values, conn)


def insert_lens_reading(values: dict, conn=None) -> int:
    return _insert("lens_readings", values, conn)


def insert_alert(kind: str, message: str, delivered: bool, error: str | None = None,
                 reading_id: int | None = None, conn=None) -> int:
    return _insert("alerts", {
        "ts": now_iso(), "kind": kind, "message": message,
        "delivered": 1 if delivered else 0, "error": error, "reading_id": reading_id,
    }, conn)


def get_setting(key: str, conn=None) -> str | None:
    with _conn(conn) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str, conn=None) -> None:
    with _conn(conn) as c:
        c.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_iso()),
        )
        c.commit()


def get_state(key: str, default: str | None = None, conn=None) -> str | None:
    with _conn(conn) as c:
        row = c.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str, conn=None) -> None:
    with _conn(conn) as c:
        c.execute(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        c.commit()


def upsert_weather(hour_ts: str, precipitation_mm: float | None, conn=None) -> None:
    with _conn(conn) as c:
        c.execute(
            "INSERT INTO weather (hour_ts, precipitation_mm, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(hour_ts) DO UPDATE SET precipitation_mm = excluded.precipitation_mm, "
            "fetched_at = excluded.fetched_at",
            (hour_ts, precipitation_mm, now_iso()),
        )
        c.commit()
