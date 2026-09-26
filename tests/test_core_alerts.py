"""Tests: alert policy."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from wlm import db
from wlm.alerts import process_alerts, maybe_send_heartbeat


def _make_result(status: str = "normal", level_index: float = 10.0, desc: str = "dry") -> dict:
    return {
        "level_status": status,
        "level_index": level_index,
        "estimated_level_description": desc,
        "distance_to_critical": "far",
        "confidence": 0.9,
        "reason": "test",
        "per_lens": [],
    }


class TestAlertPolicy:
    def test_warning_on_first_transition(self, tmp_db):
        """Warning alert fires on transition from normal → warning."""
        # Configure telegram so delivered=1 is possible
        db.set_setting("telegram_bot_token", "test_token_abc", conn=tmp_db)
        db.set_setting("telegram_chat_id", "99999", conn=tmp_db)
        result = _make_result("warning", 60.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.tg.send_photo") as mock_photo:
            mock_msg.return_value = (True, None)
            mock_photo.return_value = (True, None)
            process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='warning'").fetchall()
        assert len(rows) == 1
        assert rows[0]["delivered"] == 1

    def test_no_warning_repeat(self, tmp_db):
        """Warning alert does NOT fire again if already in warning state."""
        db.set_state("last_status", "warning", conn=tmp_db)
        result = _make_result("warning", 60.0)
        with patch("wlm.alerts.tg.send_photo") as mock_photo:
            mock_photo.return_value = (True, None)
            process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='warning'").fetchall()
        assert len(rows) == 0

    def test_recovery_after_warning(self, tmp_db):
        """Recovery alert fires when going from warning → normal."""
        db.set_state("last_status", "warning", conn=tmp_db)
        result = _make_result("normal", 10.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_msg.return_value = (True, None)
            process_alerts("normal", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='recovered'").fetchall()
        assert len(rows) == 1

    def test_warning_unknown_normal_recovery(self, tmp_db):
        """warning → unknown → normal still sends a recovered alert."""
        db.set_state("last_status", "warning", conn=tmp_db)

        # First: unknown cycle — must NOT overwrite last_status
        result_unknown = _make_result("unknown", None, "unclear")
        result_unknown["level_index"] = None
        process_alerts("unknown", result_unknown, None, None, dry_run=False, conn=tmp_db)

        last_status = db.get_state("last_status", conn=tmp_db)
        assert last_status == "warning", "unknown must not overwrite last_status"

        # Then normal — recovery should fire
        result_normal = _make_result("normal", 10.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_msg.return_value = (True, None)
            process_alerts("normal", result_normal, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='recovered'").fetchall()
        assert len(rows) == 1, "Recovery alert should fire after warning→unknown→normal"

    def test_critical_repeat_interval(self, tmp_db):
        """Critical alert repeats only after critical_repeat_minutes have elapsed."""
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(minutes=5)).isoformat()
        db.set_state("last_critical_alert_ts", recent_ts, conn=tmp_db)

        result = _make_result("critical", 92.0)
        with patch("wlm.alerts.tg.send_photo") as mock_photo:
            mock_photo.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        # Default critical_repeat_minutes = 10, only 5 min have passed → suppressed
        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(rows) == 0, "Critical should be suppressed within repeat interval"

    def test_critical_repeat_after_interval(self, tmp_db):
        """Critical alert fires again after critical_repeat_minutes."""
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        db.set_state("last_critical_alert_ts", old_ts, conn=tmp_db)

        result = _make_result("critical", 92.0)
        with patch("wlm.alerts.tg.send_photo") as mock_photo:
            mock_photo.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(rows) == 1, "Critical should fire after repeat interval"

    def test_failure_alert_after_threshold(self, tmp_db):
        """Failure alert fires after consecutive_failures >= failure_threshold."""
        db.set_state("consecutive_failures", "2", conn=tmp_db)  # threshold is 3
        result = _make_result("unknown", None)
        result["level_index"] = None

        with patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_msg.return_value = (True, None)
            process_alerts("unknown", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='failure'").fetchall()
        assert len(rows) == 1

    def test_failure_alert_sent_only_once(self, tmp_db):
        """Failure alert fires only once until recovery."""
        db.set_state("consecutive_failures", "4", conn=tmp_db)
        db.set_state("failure_alert_sent", "1", conn=tmp_db)
        result = _make_result("unknown", None)
        result["level_index"] = None

        with patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_msg.return_value = (True, None)
            process_alerts("unknown", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='failure'").fetchall()
        assert len(rows) == 0, "Failure alert should not fire again if already sent"

    def test_telegram_disabled_records_delivered_0(self, tmp_db):
        """When telegram disabled, alert is recorded with delivered=0."""
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_setting("alert_on_warning", "1", conn=tmp_db)
        result = _make_result("warning", 60.0)

        process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='warning'").fetchall()
        assert len(rows) == 1
        assert rows[0]["delivered"] == 0
        assert "telegram" in rows[0]["error"].lower()

    def test_no_token_records_delivered_0(self, tmp_db):
        """When token/chat missing, alert is recorded with delivered=0."""
        db.set_setting("telegram_enabled", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "", conn=tmp_db)
        db.set_setting("telegram_chat_id", "", conn=tmp_db)
        result = _make_result("warning", 60.0)

        process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='warning'").fetchall()
        assert len(rows) == 1
        assert rows[0]["delivered"] == 0

    def test_dry_run_no_db_writes(self, tmp_db):
        """dry_run=True: no alert rows, no state changes."""
        initial_last_status = db.get_state("last_status", conn=tmp_db)
        result = _make_result("warning", 60.0)
        process_alerts("warning", result, None, None, dry_run=True, conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM alerts").fetchall()
        assert len(rows) == 0, "dry_run must not write alert rows"
        assert db.get_state("last_status", conn=tmp_db) == initial_last_status, \
            "dry_run must not update last_status"

    def test_consecutive_failures_reset_on_success(self, tmp_db):
        """Consecutive failures reset to 0 when a non-unknown status occurs."""
        db.set_state("consecutive_failures", "5", conn=tmp_db)
        result = _make_result("normal", 10.0)
        with patch("wlm.alerts.tg.send_message"):
            process_alerts("normal", result, None, None, dry_run=False, conn=tmp_db)

        val = db.get_state("consecutive_failures", conn=tmp_db)
        assert val == "0"
