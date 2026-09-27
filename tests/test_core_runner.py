"""Tests: run_cycle, run_now_requested, dry_run."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import json

import pytest
from PIL import Image

from wlm import db
from wlm.runner import run_cycle, _should_run_now, _recheck_due, _clear_recheck_at


def _make_image(path: Path) -> None:
    img = Image.new("RGB", (100, 100), (100, 100, 100))
    img.save(str(path), "JPEG")


def _unknown_analysis():
    return (
        {
            "level_status": "unknown",
            "level_index": None,
            "estimated_level_description": "no key",
            "distance_to_critical": "unknown",
            "confidence": 0.0,
            "reason": "ANTHROPIC_API_KEY not set",
            "per_lens": [],
        },
        0, 0, None,
    )


class TestRunCycle:
    def test_stores_reading_and_lens_rows(self, tmp_db, tmp_path, monkeypatch):
        """run_cycle inserts one reading + one lens row per lens."""
        street = tmp_path / "street.jpg"
        carport = tmp_path / "carport.jpg"
        _make_image(street)
        _make_image(carport)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = (
                {
                    "level_status": "normal",
                    "level_index": 10.0,
                    "estimated_level_description": "dry",
                    "distance_to_critical": "far",
                    "confidence": 0.9,
                    "reason": "dry",
                    "per_lens": [
                        {"label": "street", "observation": "dry", "water_coverage_pct": 0.0},
                        {"label": "carport", "observation": "dry", "water_coverage_pct": 0.0},
                    ],
                },
                100, 50, 0.001,
            )
            reading_id = run_cycle(
                dry_run=False,
                images=[("street", street), ("carport", carport)],
            )

        assert reading_id is not None

        conn = db.connect()
        reading = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        assert reading is not None
        assert reading["status"] == "normal"
        assert reading["level_index"] == 10.0

        lens_rows = conn.execute(
            "SELECT * FROM lens_readings WHERE reading_id=?", (reading_id,)
        ).fetchall()
        assert len(lens_rows) == 2
        labels = {r["label"] for r in lens_rows}
        assert "street" in labels
        assert "carport" in labels
        conn.close()

    def test_failed_lens_stored_as_ok_0(self, tmp_db, tmp_path, monkeypatch):
        """A lens that fails to capture is stored with ok=0."""
        street = tmp_path / "street.jpg"
        _make_image(street)
        missing = tmp_path / "missing.jpg"  # does not exist

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = (
                {
                    "level_status": "normal",
                    "level_index": 5.0,
                    "estimated_level_description": "dry",
                    "distance_to_critical": "far",
                    "confidence": 0.8,
                    "reason": "partial",
                    "per_lens": [
                        {"label": "street", "observation": "dry", "water_coverage_pct": 0.0},
                    ],
                },
                50, 25, 0.0005,
            )
            reading_id = run_cycle(
                dry_run=False,
                images=[("street", street), ("carport", missing)],
            )

        conn = db.connect()
        lens_rows = conn.execute(
            "SELECT * FROM lens_readings WHERE reading_id=?", (reading_id,)
        ).fetchall()
        assert len(lens_rows) == 2
        ok_map = {r["label"]: r["ok"] for r in lens_rows}
        assert ok_map["street"] == 1
        assert ok_map["carport"] == 0
        conn.close()

    def test_dry_run_no_alert_rows(self, tmp_db, tmp_path, monkeypatch):
        """dry_run=True: reading is inserted but no alert rows and no state changes."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = (
                {
                    "level_status": "warning",
                    "level_index": 60.0,
                    "estimated_level_description": "rising",
                    "distance_to_critical": "30",
                    "confidence": 0.8,
                    "reason": "water on street",
                    "per_lens": [],
                },
                10, 5, 0.0001,
            )
            reading_id = run_cycle(dry_run=True, images=[("street", street)])

        assert reading_id is not None

        conn = db.connect()
        alert_rows = conn.execute("SELECT * FROM alerts").fetchall()
        assert len(alert_rows) == 0, "dry_run must not write alert rows"
        conn.close()


class TestRunNowRequested:
    """Test _should_run_now decision function."""

    def test_run_now_flag_triggers_run(self, tmp_db):
        db.set_state("run_now_requested", "2026-09-25T14:00:00+00:00", conn=tmp_db)
        # last_run_ts = just now, so interval hasn't elapsed
        last_run = time.monotonic()
        assert _should_run_now(last_run, conn=tmp_db) is True

    def test_interval_elapsed_triggers_run(self, tmp_db):
        # Set capture interval to 1 minute; simulate 70 seconds elapsed
        db.set_setting("capture_interval_minutes", "1", conn=tmp_db)
        last_run = time.monotonic() - 70
        assert _should_run_now(last_run, conn=tmp_db) is True

    def test_interval_not_elapsed_no_run(self, tmp_db):
        db.set_setting("capture_interval_minutes", "10", conn=tmp_db)
        last_run = time.monotonic() - 30  # only 30s elapsed
        db.set_state("run_now_requested", "0", conn=tmp_db)
        assert _should_run_now(last_run, conn=tmp_db) is False

    def test_first_run_always_triggers(self, tmp_db):
        """A very old last_run_ts should always trigger a run."""
        db.set_setting("capture_interval_minutes", "10", conn=tmp_db)
        db.set_state("run_now_requested", "0", conn=tmp_db)
        # Use a timestamp well in the past (10000 seconds ago) to ensure elapsed > 600
        last_run = time.monotonic() - 10000
        assert _should_run_now(last_run, conn=tmp_db) is True

    def test_due_recheck_at_triggers_run_and_is_cleared(self, tmp_db):
        """A recheck_at in the past makes _should_run_now return True; clear removes it."""
        from datetime import datetime, timezone, timedelta
        db.set_setting("capture_interval_minutes", "10", conn=tmp_db)
        db.set_state("run_now_requested", "0", conn=tmp_db)
        # Set recheck_at to 10 seconds ago (due)
        past_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
        db.set_state("recheck_at", past_ts, conn=tmp_db)
        last_run = time.monotonic() - 30  # interval not elapsed

        assert _recheck_due(conn=tmp_db) is True
        assert _should_run_now(last_run, conn=tmp_db) is True

        # Clearing recheck_at removes the trigger
        _clear_recheck_at(conn=tmp_db)
        assert _recheck_due(conn=tmp_db) is False
        assert _should_run_now(last_run, conn=tmp_db) is False

    def test_not_due_recheck_at_does_not_trigger(self, tmp_db):
        """A recheck_at in the future does not trigger a run early."""
        from datetime import datetime, timezone, timedelta
        db.set_setting("capture_interval_minutes", "10", conn=tmp_db)
        db.set_state("run_now_requested", "0", conn=tmp_db)
        # Set recheck_at to 60 seconds in the future (not yet due)
        future_ts = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
        db.set_state("recheck_at", future_ts, conn=tmp_db)
        last_run = time.monotonic() - 30  # interval not elapsed

        assert _recheck_due(conn=tmp_db) is False
        assert _should_run_now(last_run, conn=tmp_db) is False
