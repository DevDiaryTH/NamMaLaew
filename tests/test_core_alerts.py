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
        # System is already in confirmed critical state, so no re-confirmation is required.
        db.set_state("last_status", "critical", conn=tmp_db)

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
        # System is already in confirmed critical state, so no re-confirmation is required.
        db.set_state("last_status", "critical", conn=tmp_db)

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


class TestCriticalConfirm:
    """Tests for the critical confirmation (two-reading) feature."""

    def test_first_critical_no_telegram_no_siren_pending_row_recheck_set(self, tmp_db):
        """First CRITICAL with confirm on: no Telegram, no siren, pending row, recheck_at set."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        db.set_setting("critical_confirm_recheck_seconds", "120", conn=tmp_db)
        result = _make_result("critical", 92.0)

        with patch("wlm.alerts.tg.send_photo") as mock_photo, \
             patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_photo.return_value = (True, None)
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        # No Telegram sent
        mock_photo.assert_not_called()
        mock_msg.assert_not_called()

        # Pending alert row recorded (delivered=0)
        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='pending'").fetchall()
        assert len(rows) == 1
        assert rows[0]["delivered"] == 0
        assert "re-checking" in rows[0]["message"]

        # No real critical alert row
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 0

        # critical_pending is set
        pending_val = db.get_state("critical_pending", conn=tmp_db)
        assert pending_val, "critical_pending should be set after first CRITICAL"

        # recheck_at is set
        recheck_at = db.get_state("recheck_at", conn=tmp_db)
        assert recheck_at, "recheck_at should be set after first CRITICAL"

        # last_status NOT updated to critical
        last_status = db.get_state("last_status", conn=tmp_db)
        assert last_status != "critical", "last_status must not flip to critical while pending"

    def test_second_consecutive_critical_sends_alert_clears_pending(self, tmp_db):
        """Second consecutive CRITICAL: Telegram + siren fire once, pending cleared."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        # Simulate pending state from first critical
        db.set_state("critical_pending", "2026-09-26T06:00:00+00:00", conn=tmp_db)
        result = _make_result("critical", 92.0)

        # photo_path=None → _send_alert uses send_message (not send_photo)
        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        # Telegram alert sent exactly once
        mock_msg.assert_called_once()

        # Critical alert row exists
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 1

        # critical_pending cleared
        pending_val = db.get_state("critical_pending", conn=tmp_db)
        assert not pending_val, "critical_pending must be cleared after confirmation"

        # last_status updated to critical
        assert db.get_state("last_status", conn=tmp_db) == "critical"

    def test_first_critical_then_warning_no_alert_not_confirmed_row_pending_cleared(self, tmp_db):
        """First CRITICAL then WARNING re-check: no critical alert, 'not confirmed' row, pending cleared."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        db.set_state("last_status", "warning", conn=tmp_db)
        # Simulate pending from first critical
        db.set_state("critical_pending", "2026-09-26T06:00:00+00:00", conn=tmp_db)
        result = _make_result("warning", 60.0)

        with patch("wlm.alerts.tg.send_photo") as mock_photo, \
             patch("wlm.alerts.tg.send_message") as mock_msg:
            mock_photo.return_value = (True, None)
            mock_msg.return_value = (True, None)
            process_alerts("warning", result, None, None, dry_run=False, conn=tmp_db)

        # No critical alert
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 0

        # "not confirmed" pending row recorded
        pend_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='pending'").fetchall()
        assert len(pend_rows) == 1
        assert "not confirmed" in pend_rows[0]["message"]
        assert "warning" in pend_rows[0]["message"]

        # critical_pending cleared
        pending_val = db.get_state("critical_pending", conn=tmp_db)
        assert not pending_val, "critical_pending must be cleared after non-critical re-check"

    def test_first_critical_then_unknown_not_confirmed(self, tmp_db):
        """Re-check returning unknown also clears pending without alerting."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        db.set_state("critical_pending", "2026-09-26T06:00:00+00:00", conn=tmp_db)
        result = _make_result("unknown", None)
        result["level_index"] = None

        process_alerts("unknown", result, None, None, dry_run=False, conn=tmp_db)

        # No critical alert
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 0

        # "not confirmed" pending row
        pend_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='pending'").fetchall()
        assert len(pend_rows) == 1
        assert "not confirmed" in pend_rows[0]["message"]

        # critical_pending cleared
        pending_val = db.get_state("critical_pending", conn=tmp_db)
        assert not pending_val

    def test_critical_confirm_off_immediate_alert(self, tmp_db):
        """When critical_confirm is off, a CRITICAL reading fires immediately (old behaviour)."""
        db.set_setting("critical_confirm", "0", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        result = _make_result("critical", 92.0)

        # photo_path=None → _send_alert uses send_message (not send_photo)
        with patch("wlm.alerts.tg.send_message") as mock_msg, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_msg.return_value = (True, None)
            process_alerts("critical", result, None, None, dry_run=False, conn=tmp_db)

        # Immediate Telegram send
        mock_msg.assert_called_once()

        # Critical alert row
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 1

        # No pending row
        pend_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='pending'").fetchall()
        assert len(pend_rows) == 0

        # critical_pending not set
        pending_val = db.get_state("critical_pending", conn=tmp_db)
        assert not pending_val

    def test_critical_repeat_still_suppresses_after_confirmation(self, tmp_db):
        """critical_repeat_minutes still suppresses a second Telegram after confirmation."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        # Already in confirmed critical state (last_status=critical, last_critical_alert recent)
        now = datetime.now(timezone.utc)
        db.set_state("last_status", "critical", conn=tmp_db)
        recent_ts = (now - timedelta(minutes=5)).isoformat()
        db.set_state("last_critical_alert_ts", recent_ts, conn=tmp_db)
        result = _make_result("critical", 92.0)

        with patch("wlm.alerts.tg.send_photo") as mock_photo, \
             patch("wlm.alerts.siren.is_configured", return_value=False):
            mock_photo.return_value = (True, None)
            process_alerts("critical", result, 1, None, dry_run=False, conn=tmp_db)

        # Suppressed by critical_repeat (5 min < 10 min default)
        mock_photo.assert_not_called()
        crit_rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='critical'").fetchall()
        assert len(crit_rows) == 0

    def test_dry_run_critical_confirm_no_state_change(self, tmp_db):
        """dry_run with critical_confirm on: no state changes, no DB writes."""
        db.set_setting("critical_confirm", "1", conn=tmp_db)
        initial_pending = db.get_state("critical_pending", conn=tmp_db) or ""
        initial_recheck = db.get_state("recheck_at", conn=tmp_db) or ""
        result = _make_result("critical", 92.0)

        process_alerts("critical", result, 1, None, dry_run=True, conn=tmp_db)

        # State unchanged
        assert (db.get_state("critical_pending", conn=tmp_db) or "") == initial_pending
        assert (db.get_state("recheck_at", conn=tmp_db) or "") == initial_recheck
        # No alert rows
        rows = tmp_db.execute("SELECT * FROM alerts").fetchall()
        assert len(rows) == 0
