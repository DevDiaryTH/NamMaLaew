"""Shared fixtures for web dashboard tests.

Note: do NOT put shared fixtures in conftest.py (owned by the monitor worker).
Import from this module explicitly in each test file, or use the fixtures
after importing this module.
"""

from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient, ASGITransport

# ---------------------------------------------------------------------------
# Make sure wlm package is importable
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# In-memory or temp-file SQLite DB
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database using the WLM schema."""
    from wlm import db as wlm_db
    db_file = tmp_path / "test.db"
    conn = wlm_db.connect(db_file)
    return conn, db_file


@pytest.fixture
def tmp_snap_dir(tmp_path):
    snap = tmp_path / "snaps"
    snap.mkdir()
    return snap


@pytest.fixture
def demo_db(tmp_db, tmp_snap_dir):
    """A DB seeded with a small set of realistic readings."""
    from wlm import db as wlm_db
    conn, db_file = tmp_db

    now = datetime.now(timezone.utc)

    # Create a minimal JPEG for snapshot testing (1x1 gray JPEG)
    MINIMAL_JPEG = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD2, 0x8A, 0x28, 0x03, 0xFF, 0xD9,
    ])
    # Write a snap for the "street" lens (first reading) so the /lines page renders canvas
    street_snap = tmp_snap_dir / "street_test.jpg"
    street_snap.write_bytes(MINIMAL_JPEG)
    street_snap_rel = "street_test.jpg"

    # Insert a few normal readings
    for i in range(5):
        ts = (now - timedelta(minutes=10 * (5 - i))).isoformat(timespec="seconds")
        rid = wlm_db.insert_reading({
            "ts": ts,
            "status": "normal",
            "model_status": "normal",
            "level_index": 15.0 + i,
            "confidence": 0.9,
            "description": "Normal",
            "reason": "All clear",
            "composite_path": None,
            "model": "claude-test",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cost_usd": 0.0015,
            "capture_ms": 1000,
            "analysis_ms": 5000,
            "error": None,
        }, conn=conn)

        for label in ("street", "carport"):
            # "street" gets a real snapshot file; "carport" has no snapshot (tests both paths)
            snap_path = street_snap_rel if (label == "street" and i == 4) else None
            wlm_db.insert_lens_reading({
                "reading_id": rid,
                "label": label,
                "snapshot_path": snap_path,
                "ok": 1,
                "observation": "Clear",
                "water_coverage_pct": 5.0,
                "brightness": 120.0,
                "sharpness": 80.0,
                "is_night": 0,
                "error": None,
                "line_position": "below_warning",
            }, conn=conn)

    # Insert a rising event
    for i in range(3):
        ts = (now - timedelta(minutes=30 - i * 10)).isoformat(timespec="seconds")
        level = 55.0 + i * 15
        status = "warning" if level < 90 else "critical"
        rid = wlm_db.insert_reading({
            "ts": ts,
            "status": status,
            "model_status": status,
            "level_index": level,
            "confidence": 0.85,
            "description": f"Level {level}",
            "reason": "Rising",
            "composite_path": None,
            "model": "claude-test",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cost_usd": 0.0015,
            "capture_ms": 1000,
            "analysis_ms": 5000,
            "error": None,
        }, conn=conn)
        # Add lens reading with line_position for rise readings
        for label in ("street", "carport"):
            lp = "at_or_above_critical" if level >= 90 else "at_or_above_warning"
            wlm_db.insert_lens_reading({
                "reading_id": rid,
                "label": label,
                "snapshot_path": None,
                "ok": 1,
                "observation": "Rising water",
                "water_coverage_pct": 40.0,
                "brightness": 110.0,
                "sharpness": 70.0,
                "is_night": 0,
                "error": None,
                "line_position": lp,
            }, conn=conn)

    # Insert an unknown reading
    unknown_rid = wlm_db.insert_reading({
        "ts": (now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        "status": "unknown",
        "model_status": None,
        "level_index": None,
        "confidence": None,
        "description": None,
        "reason": None,
        "composite_path": None,
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "capture_ms": 1000,
        "analysis_ms": None,
        "error": "Timeout",
    }, conn=conn)
    for label in ("street", "carport"):
        wlm_db.insert_lens_reading({
            "reading_id": unknown_rid,
            "label": label,
            "snapshot_path": None,
            "ok": 0,
            "observation": None,
            "water_coverage_pct": None,
            "brightness": None,
            "sharpness": None,
            "is_night": 0,
            "error": "Timeout",
            "line_position": "not_visible",
        }, conn=conn)

    # Insert an alert
    wlm_db.insert_alert("warning", "Test warning", True, conn=conn)
    wlm_db.insert_alert("test", "Test Telegram", True, conn=conn)

    wlm_db.set_setting("level_warning", "50", conn=conn)
    wlm_db.set_setting("level_critical", "90", conn=conn)
    wlm_db.set_setting("capture_interval_minutes", "10", conn=conn)

    conn.commit()
    return conn, db_file, tmp_snap_dir


# ---------------------------------------------------------------------------
# App client factory
# ---------------------------------------------------------------------------

@pytest.fixture
def app_env(demo_db, monkeypatch):
    """Set env vars so the app uses the test DB and snapshot dir."""
    conn, db_file, snap_dir = demo_db
    conn.close()  # app will open its own connection
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap_dir))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass")
    monkeypatch.setenv("DASHBOARD_SECRET", "testsecret1234567890abcdef1234567890abcdef")
    return db_file, snap_dir


@pytest.fixture
def no_password_env(demo_db, monkeypatch):
    conn, db_file, snap_dir = demo_db
    conn.close()
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap_dir))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_SECRET", "testsecret1234567890abcdef1234567890abcdef")
    return db_file, snap_dir


@pytest.fixture
def client(app_env):
    """Synchronous-style client helper — returns an async client context."""
    return app_env


async def make_client(app_env):
    """Helper to create an async test client."""
    import importlib
    import web.app
    importlib.reload(web.app)
    from web.app import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def get_session_cookie(client, password="testpass"):
    """Log in and return the session cookie string."""
    # First get the CSRF token
    resp = await client.get("/login")
    assert resp.status_code == 200
    # Extract CSRF from form
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    assert m, "CSRF token not found in login form"
    csrf = m.group(1)
    # Copy cookies from login page (session cookie)
    login_resp = await client.post(
        "/login",
        data={"password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert login_resp.status_code == 302
    return login_resp.cookies
