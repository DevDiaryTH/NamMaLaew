"""Shared fixtures for wlm core tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from wlm import db


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
