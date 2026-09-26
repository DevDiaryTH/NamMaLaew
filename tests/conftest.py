"""Shared fixtures for wlm core tests."""

from __future__ import annotations

import os
import tempfile
import urllib.request
from pathlib import Path

import pytest

from wlm import db
from wlm import settings as wlm_settings


@pytest.fixture(autouse=True)
def _isolate_from_host_env(monkeypatch):
    """Keep the developer's shell env from leaking into tests.

    settings.get() falls back to the env var when the DB value is empty, so a
    real TELEGRAM_BOT_TOKEN / MQTT_HOST exported in the shell would make tests
    message a real chat or reach a real broker.
    """
    for spec in wlm_settings.SPECS:
        if spec.env:
            monkeypatch.delenv(spec.env, raising=False)

    # Backstop: block Telegram calls and fail the test afterwards. Raising alone
    # isn't enough, since telegram.send_message swallows every exception.
    real_urlopen = urllib.request.urlopen
    telegram_calls = []

    def guarded_urlopen(req, *args, **kwargs):
        url = str(getattr(req, "full_url", req))
        if "api.telegram.org" in url:
            telegram_calls.append(url.split("/bot", 1)[0])
            raise OSError("real Telegram API blocked in tests")
        return real_urlopen(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    yield
    if telegram_calls:
        pytest.fail(f"test tried to reach the real Telegram API {len(telegram_calls)} time(s)")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Provide a fresh SQLite DB in a temp directory and set WLM_DB + WLM_SNAPSHOT_DIR."""
    db_file = tmp_path / "test.db"
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap_dir))
    conn = db.connect(db_file)
    yield conn
    conn.close()


@pytest.fixture
def snap_dir(tmp_path, monkeypatch):
    """Set WLM_SNAPSHOT_DIR to a fresh temp directory."""
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(d))
    return d
