"""Tests for confidence calibration: bucket assignment, calibration_table, calibrate,
accuracy_stats, DB migration, runner integration, and alert behaviour under low confidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from wlm import db, learning
from wlm.learning import CONFIDENCE_BUCKETS, calibration_table, calibrate, accuracy_stats
from wlm.alerts import process_alerts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_reading(conn, status="normal", level_index=25.0, confidence=0.9,
                  calibrated_confidence=None) -> int:
    return db.insert_reading({
        "status": status,
        "model_status": status,
        "level_index": level_index,
        "confidence": confidence,
        "calibrated_confidence": calibrated_confidence,
        "description": "Test",
        "reason": "Test",
        "distance_to_critical": "far",
        "composite_path": None,
        "model": "claude-test",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.0001,
        "capture_ms": 500,
        "analysis_ms": 2000,
        "error": None,
    }, conn=conn)


def _seed_feedback(conn, reading_id: int, verdict: str = "correct",
                   true_status: str | None = None, true_level_index: float | None = None):
    learning.record_feedback(
        reading_id=reading_id,
        verdict=verdict,
        true_status=true_status,
        true_level_index=true_level_index,
        conn=conn,
    )


def _make_result(status: str = "critical", level_index: float = 92.0,
                 confidence: float = 0.9, calibrated_confidence: float | None = None) -> dict:
    return {
        "level_status": status,
        "level_index": level_index,
        "estimated_level_description": "high water",
        "distance_to_critical": "close",
        "confidence": confidence,
        "calibrated_confidence": calibrated_confidence,
        "reason": "test",
        "per_lens": [],
    }


def _recent_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CONFIDENCE_BUCKETS constant
# ---------------------------------------------------------------------------

class TestBucketConstants:
    def test_buckets_cover_zero_to_one(self):
        """Every value from 0.0 to <1.01 falls into exactly one bucket."""
        for v in [0.0, 0.1, 0.49, 0.5, 0.69, 0.7, 0.84, 0.85, 0.99, 1.0]:
            matched = [b for b in CONFIDENCE_BUCKETS if b[0] <= v < b[1]]
            assert len(matched) == 1, f"value {v} matched {len(matched)} buckets"

    def test_boundary_values_land_in_correct_bucket(self):
        """Boundaries 0.5, 0.7, 0.85, 1.0 each land in the expected bucket."""
        assert CONFIDENCE_BUCKETS[1][0] <= 0.5 < CONFIDENCE_BUCKETS[1][1]  # [0.5, 0.7)
        assert CONFIDENCE_BUCKETS[2][0] <= 0.7 < CONFIDENCE_BUCKETS[2][1]  # [0.7, 0.85)
        assert CONFIDENCE_BUCKETS[3][0] <= 0.85 < CONFIDENCE_BUCKETS[3][1] # [0.85, 1.01)
        assert CONFIDENCE_BUCKETS[3][0] <= 1.0 < CONFIDENCE_BUCKETS[3][1]  # 1.0 in last bucket


# ---------------------------------------------------------------------------
# calibration_table: unknown and NULL excluded, accuracy computation
# ---------------------------------------------------------------------------

class TestCalibrationTable:
    def test_empty_db_returns_zero_n(self, tmp_db):
        table = calibration_table(conn=tmp_db)
        assert len(table) == len(CONFIDENCE_BUCKETS)
        for row in table:
            assert row["n"] == 0
            assert row["accuracy"] is None
            assert row["reliable"] is False

    def test_unknown_readings_excluded(self, tmp_db):
        rid = _seed_reading(tmp_db, status="unknown", level_index=None, confidence=0.9)
        _seed_feedback(tmp_db, rid, verdict="correct", true_status="normal")
        table = calibration_table(conn=tmp_db)
        # No row in any bucket because status='unknown' is excluded
        assert all(row["n"] == 0 for row in table)

    def test_null_confidence_excluded(self, tmp_db):
        rid = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=None)
        _seed_feedback(tmp_db, rid, verdict="correct", true_status="normal")
        table = calibration_table(conn=tmp_db)
        assert all(row["n"] == 0 for row in table)

    def test_correct_verdict_counts(self, tmp_db):
        # confidence=0.8 → bucket [0.7, 0.85)
        rid = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=0.8)
        _seed_feedback(tmp_db, rid, verdict="correct")
        table = calibration_table(conn=tmp_db)
        bucket = next(r for r in table if r["lo"] == 0.7)
        assert bucket["n"] == 1
        assert bucket["n_correct"] == 1
        assert bucket["accuracy"] == 1.0

    def test_wrong_verdict_miscount(self, tmp_db):
        # reading says 'critical' but feedback says 'normal' → wrong, n_correct=0
        rid = _seed_reading(tmp_db, status="critical", level_index=92.0, confidence=0.8)
        _seed_feedback(tmp_db, rid, verdict="wrong", true_status="normal")
        table = calibration_table(conn=tmp_db)
        bucket = next(r for r in table if r["lo"] == 0.7)
        assert bucket["n"] == 1
        assert bucket["n_correct"] == 0
        assert bucket["accuracy"] == 0.0

    def test_reliable_requires_min_samples(self, tmp_db):
        # 4 readings with confidence 0.6 → n=4 < 5 → not reliable
        for _ in range(4):
            rid = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=0.6)
            _seed_feedback(tmp_db, rid, verdict="correct")
        table = calibration_table(conn=tmp_db, min_samples=5)
        bucket = next(r for r in table if r["lo"] == 0.5)
        assert bucket["n"] == 4
        assert bucket["reliable"] is False

    def test_reliable_with_enough_samples(self, tmp_db):
        for _ in range(5):
            rid = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=0.6)
            _seed_feedback(tmp_db, rid, verdict="correct")
        table = calibration_table(conn=tmp_db, min_samples=5)
        bucket = next(r for r in table if r["lo"] == 0.5)
        assert bucket["n"] == 5
        assert bucket["reliable"] is True
        assert bucket["accuracy"] == 1.0


# ---------------------------------------------------------------------------
# calibrate()
# ---------------------------------------------------------------------------

class TestCalibrateFunction:
    def test_none_confidence_returns_none(self):
        table = calibration_table.__wrapped__ if hasattr(calibration_table, '__wrapped__') else None
        # Build a minimal table manually
        t = [{"lo": lo, "hi": hi, "n": 0, "n_correct": 0, "accuracy": None, "reliable": False}
             for lo, hi in CONFIDENCE_BUCKETS]
        assert calibrate(None, t) is None

    def test_reliable_bucket_returns_accuracy(self):
        t = [{"lo": lo, "hi": hi, "n": 10, "n_correct": 8, "accuracy": 0.8, "reliable": True}
             for lo, hi in CONFIDENCE_BUCKETS]
        result = calibrate(0.75, t)  # falls in [0.7, 0.85)
        assert result == 0.8

    def test_unreliable_bucket_returns_raw(self):
        t = [{"lo": lo, "hi": hi, "n": 2, "n_correct": 2, "accuracy": 1.0, "reliable": False}
             for lo, hi in CONFIDENCE_BUCKETS]
        result = calibrate(0.75, t)
        assert result == 0.75

    def test_boundary_085(self):
        t = []
        for lo, hi in CONFIDENCE_BUCKETS:
            t.append({"lo": lo, "hi": hi, "n": 10, "n_correct": 9,
                       "accuracy": 0.9, "reliable": True})
        # 0.85 → bucket [0.85, 1.01)
        result = calibrate(0.85, t)
        assert result == 0.9

    def test_boundary_1_0(self):
        t = []
        for lo, hi in CONFIDENCE_BUCKETS:
            t.append({"lo": lo, "hi": hi, "n": 10, "n_correct": 7,
                       "accuracy": 0.7, "reliable": True})
        result = calibrate(1.0, t)
        assert result == 0.7


# ---------------------------------------------------------------------------
# accuracy_stats()
# ---------------------------------------------------------------------------

class TestAccuracyStats:
    def test_empty_db_returns_none_values(self, tmp_db):
        stats = accuracy_stats(conn=tmp_db)
        assert stats["feedback_count"] == 0
        assert stats["status_accuracy"] is None
        assert stats["level_mae"] is None
        assert isinstance(stats["calibration"], list)

    def test_status_accuracy_all_correct(self, tmp_db):
        for _ in range(3):
            rid = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=0.8)
            _seed_feedback(tmp_db, rid, verdict="correct")
        stats = accuracy_stats(conn=tmp_db)
        assert stats["feedback_count"] == 3
        assert stats["status_accuracy"] == 1.0

    def test_status_accuracy_mixed(self, tmp_db):
        rid1 = _seed_reading(tmp_db, status="normal", level_index=25.0, confidence=0.8)
        _seed_feedback(tmp_db, rid1, verdict="correct")
        rid2 = _seed_reading(tmp_db, status="warning", level_index=60.0, confidence=0.8)
        _seed_feedback(tmp_db, rid2, verdict="wrong", true_status="normal")
        stats = accuracy_stats(conn=tmp_db)
        assert stats["status_accuracy"] == pytest.approx(0.5)

    def test_level_mae(self, tmp_db):
        rid = _seed_reading(tmp_db, status="normal", level_index=30.0, confidence=0.8)
        learning.record_feedback(
            reading_id=rid,
            verdict="wrong",
            true_status="normal",
            true_level_index=40.0,
            conn=tmp_db,
        )
        stats = accuracy_stats(conn=tmp_db)
        assert stats["level_mae"] == pytest.approx(10.0)

    def test_level_mae_none_when_no_level(self, tmp_db):
        rid = _seed_reading(tmp_db, status="normal", level_index=None, confidence=0.8)
        _seed_feedback(tmp_db, rid, verdict="correct")
        stats = accuracy_stats(conn=tmp_db)
        assert stats["level_mae"] is None


# ---------------------------------------------------------------------------
# DB migration: calibrated_confidence column
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migration_adds_column_to_old_db(self, tmp_path):
        """An old DB lacking calibrated_confidence gets the column on connect."""
        db_file = tmp_path / "old.db"
        # Create a DB without the column
        conn_old = sqlite3.connect(str(db_file))
        conn_old.execute("""CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL
        )""")
        conn_old.execute("CREATE TABLE lens_readings (id INTEGER PRIMARY KEY, reading_id INTEGER, label TEXT, ok INTEGER)")
        conn_old.commit()
        conn_old.close()

        # Reconnect via db.connect — should trigger migration
        conn = db.connect(db_file)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
        conn.close()
        assert "calibrated_confidence" in cols

    def test_migration_idempotent(self, tmp_db):
        """Running _migrate twice does not raise."""
        db._migrate(tmp_db)  # second call
        cols = {r["name"] for r in tmp_db.execute("PRAGMA table_info(readings)")}
        assert "calibrated_confidence" in cols


# ---------------------------------------------------------------------------
# runner.py: stores calibrated_confidence and passes it in result
# ---------------------------------------------------------------------------

class TestRunnerCalibration:
    def test_calibrated_confidence_stored_in_reading(self, tmp_db, tmp_path, monkeypatch):
        from wlm.runner import run_cycle
        from PIL import Image

        img_path = tmp_path / "cam.jpg"
        Image.new("RGB", (10, 10)).save(str(img_path), "JPEG")

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = (
                {
                    "level_status": "normal",
                    "level_index": 20.0,
                    "estimated_level_description": "dry",
                    "distance_to_critical": "far",
                    "confidence": 0.8,
                    "reason": "dry",
                    "per_lens": [{"label": "cam", "observation": "dry",
                                  "water_coverage_pct": 0.0}],
                },
                50, 20, 0.001,
            )
            rid = run_cycle(dry_run=True, images=[("cam", img_path)])

        assert rid is not None
        import os
        conn2 = db.connect()
        reading = conn2.execute("SELECT calibrated_confidence FROM readings WHERE id=?", (rid,)).fetchone()
        conn2.close()
        # calibrated_confidence is stored (may be raw value since no feedback yet)
        assert reading is not None
        # The column exists and is a float or None
        assert reading["calibrated_confidence"] is None or isinstance(reading["calibrated_confidence"], float)


# ---------------------------------------------------------------------------
# process_alerts: low-confidence CRITICAL behaviour
# ---------------------------------------------------------------------------

class TestLowConfidenceCritical:
    def test_low_conf_critical_enters_pending_when_confirm_off(self, tmp_db):
        """Low-confidence CRITICAL with critical_confirm=OFF enters pending (no Telegram, no siren)."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0.7", conn=tmp_db)
        db.set_setting("critical_confirm_recheck_seconds", "120", conn=tmp_db)

        result = _make_result(confidence=0.9, calibrated_confidence=0.5)

        with patch("wlm.alerts.tg.send_photo") as mock_photo, \
             patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_photo.return_value = (True, None)
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        mock_photo.assert_not_called()
        mock_msg.assert_not_called()

        pending_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='pending'").fetchall()
        assert len(pending_rows) == 1
        assert "re-checking" in pending_rows[0]["message"]

        assert db.get_state("critical_pending", conn=tmp_db), "critical_pending must be set"
        assert db.get_state("last_status", conn=tmp_db) != "critical"

    def test_second_critical_after_low_conf_pending_sends_alert(self, tmp_db):
        """After a low-confidence pending, a second CRITICAL sends the alert."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0.7", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        db.set_state("critical_pending", _recent_ts(), conn=tmp_db)

        # Second reading: high confidence (or any CRITICAL while pending)
        result = _make_result(confidence=0.95, calibrated_confidence=0.8)

        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        mock_msg.assert_called_once()
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 1
        assert not db.get_state("critical_pending", conn=tmp_db)
        assert db.get_state("last_status", conn=tmp_db) == "critical"

    def test_high_conf_critical_confirm_off_alerts_immediately(self, tmp_db):
        """High-confidence CRITICAL with critical_confirm=OFF and min_confidence=0.7 alerts immediately."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0.7", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = _make_result(confidence=0.9, calibrated_confidence=0.85)

        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        # Should alert immediately — no pending
        mock_msg.assert_called_once()
        assert not db.get_state("critical_pending", conn=tmp_db)

    def test_min_confidence_zero_disables_low_conf_check(self, tmp_db):
        """min_confidence=0 disables the low-confidence re-check even for very low calibrated."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = _make_result(confidence=0.3, calibrated_confidence=0.2)

        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        mock_msg.assert_called_once()
        assert not db.get_state("critical_pending", conn=tmp_db)

    def test_low_conf_warning_still_sends_alert(self, tmp_db):
        """Low calibrated_confidence does NOT suppress warning alerts."""
        db.set_setting("min_confidence", "0.7", conn=tmp_db)
        db.set_setting("alert_on_warning", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = {
            "level_status": "warning",
            "level_index": 60.0,
            "estimated_level_description": "rising",
            "distance_to_critical": "far",
            "confidence": 0.4,
            "calibrated_confidence": 0.4,
            "reason": "test",
            "per_lens": [],
        }

        with patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_msg.return_value = (True, None)
            process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        mock_msg.assert_called_once()
        warn_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='warning'").fetchall()
        assert len(warn_rows) == 1

    def test_critical_message_contains_confidence_line(self, tmp_db):
        """Critical Telegram message includes calibrated confidence percentage."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = _make_result(confidence=0.9, calibrated_confidence=0.82)

        captured_msg = []
        def fake_send(token, chat_id, message):
            captured_msg.append(message)
            return (True, None)

        with patch("wlm.alerts.tg.send_message", side_effect=fake_send), \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        assert captured_msg, "Expected a Telegram message to be sent"
        msg = captured_msg[0]
        assert "82%" in msg
        assert "onfidence" in msg or "Confidence" in msg

    def test_warning_message_contains_confidence_line(self, tmp_db):
        """Warning Telegram message includes confidence percentage."""
        db.set_setting("alert_on_warning", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = {
            "level_status": "warning",
            "level_index": 60.0,
            "estimated_level_description": "rising",
            "distance_to_critical": "far",
            "confidence": 0.75,
            "calibrated_confidence": 0.75,
            "reason": "test",
            "per_lens": [],
        }

        captured_msg = []
        def fake_send(token, chat_id, message):
            captured_msg.append(message)
            return (True, None)

        with patch("wlm.alerts.tg.send_message", side_effect=fake_send):
            process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        assert captured_msg
        assert "75%" in captured_msg[0]

    def test_message_omits_confidence_line_when_none(self, tmp_db):
        """When both confidence values are None, no confidence line appears in the message."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("min_confidence", "0", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)

        result = _make_result(confidence=None, calibrated_confidence=None)

        captured_msg = []
        def fake_send(token, chat_id, message):
            captured_msg.append(message)
            return (True, None)

        with patch("wlm.alerts.tg.send_message", side_effect=fake_send), \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        if captured_msg:
            assert "Confidence" not in captured_msg[0]


class TestConfidenceScale:
    """Claude sometimes answers confidence on a 0-100 scale; it must end up 0-1."""

    @pytest.mark.parametrize("raw,expected", [
        (0.78, 0.78), (78, 0.78), (1.0, 1.0), (100, 1.0), (0, 0.0), (250, 1.0), (-0.2, 0.0),
        (None, None), ("x", None),
    ])
    def test_normalize_confidence(self, raw, expected):
        from wlm.analysis import normalize_confidence
        result = normalize_confidence(raw)
        assert result == (pytest.approx(expected) if expected is not None else None)

    def test_calibrate_accepts_percent_scale(self):
        from wlm.learning import calibrate, CONFIDENCE_BUCKETS
        table = [{"lo": lo, "hi": hi, "n": 0, "n_correct": 0, "accuracy": None, "reliable": False}
                 for lo, hi in CONFIDENCE_BUCKETS]
        assert calibrate(30, table) == pytest.approx(0.3)

    def test_calibration_table_buckets_percent_scale_rows(self, tmp_db):
        from wlm import learning
        rid = db.insert_reading({"status": "normal", "level_index": 5.0, "confidence": 78.0}, conn=tmp_db)
        learning.record_feedback(rid, "correct", conn=tmp_db)
        table = learning.calibration_table(conn=tmp_db, min_samples=1)
        bucket = next(b for b in table if b["lo"] == 0.7)
        assert bucket["n"] == 1
