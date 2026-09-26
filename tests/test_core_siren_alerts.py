"""Tests: siren integration inside process_alerts."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from wlm import db
from wlm.alerts import process_alerts


def _make_result(status: str = "normal", level_index: float = 10.0) -> dict:
    return {
        "level_status": status,
        "level_index": level_index,
        "estimated_level_description": "test desc",
        "distance_to_critical": "far",
        "confidence": 0.9,
        "reason": "test reason",
        "per_lens": [],
    }


def _siren_rows(conn):
    return conn.execute("SELECT * FROM alerts WHERE kind='siren'").fetchall()


class TestSirenAlerts:
    def test_critical_sounds_siren(self, tmp_db):
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("siren_seconds", "60", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        # System already in confirmed critical state — no re-confirmation needed.
        db.set_state("last_status", "critical", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            mock_sound.return_value = (True, None)
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_called_once_with(60, conn=tmp_db, dry_run=False)
        rows = _siren_rows(tmp_db)
        assert len(rows) == 1
        assert "critical" in rows[0]["message"]
        assert rows[0]["delivered"] == 1

    def test_critical_respects_repeat_minutes(self, tmp_db):
        """Second critical within repeat window → Telegram suppressed → siren also NOT fired."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("critical_repeat_minutes", "10", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)

        # Simulate a recent critical alert (already in confirmed critical state)
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec="seconds")
        db.set_state("last_critical_alert_ts", recent_ts, conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            mock_sound.return_value = (True, None)
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        # should_alert is False → siren.sound should NOT be called
        mock_sound.assert_not_called()

    def test_warning_sounds_siren_when_enabled(self, tmp_db):
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("siren_on_warning", "1", conn=tmp_db)
        db.set_setting("alert_on_warning", "1", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound, \
             patch("wlm.alerts.tg.send_message"):
            mock_sound.return_value = (True, None)
            process_alerts("warning", _make_result("warning", 60.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_called_once()
        rows = _siren_rows(tmp_db)
        assert len(rows) == 1
        assert "warning" in rows[0]["message"]

    def test_warning_does_not_sound_when_siren_on_warning_off(self, tmp_db):
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("siren_on_warning", "0", conn=tmp_db)
        db.set_setting("alert_on_warning", "1", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound, \
             patch("wlm.alerts.tg.send_message"):
            mock_sound.return_value = (True, None)
            process_alerts("warning", _make_result("warning", 60.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_not_called()

    def test_recovery_stops_siren_when_active(self, tmp_db):
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("alert_on_recovery", "1", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)
        db.set_state("siren_off_at", "2099-01-01T00:00:00+00:00", conn=tmp_db)

        with patch("wlm.alerts.siren.stop") as mock_stop, \
             patch("wlm.alerts.tg.send_message"):
            mock_stop.return_value = (True, None)
            process_alerts("normal", _make_result("normal", 10.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_stop.assert_called_once()
        rows = _siren_rows(tmp_db)
        assert any("OFF" in r["message"] for r in rows)

    def test_recovery_does_not_stop_siren_when_not_active(self, tmp_db):
        """If siren_off_at is empty (siren was never on), stop() is not called."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("alert_on_recovery", "1", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "warning", conn=tmp_db)
        # siren_off_at not set

        with patch("wlm.alerts.siren.stop") as mock_stop, \
             patch("wlm.alerts.tg.send_message"):
            mock_stop.return_value = (True, None)
            process_alerts("normal", _make_result("normal", 10.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_stop.assert_not_called()

    def test_dry_run_does_not_publish(self, tmp_db):
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        # System already in confirmed critical state so the siren path is exercised.
        db.set_state("last_status", "critical", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            mock_sound.return_value = (True, None)
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=True, conn=tmp_db)

        # sound() is called with dry_run=True (prints only, no DB write)
        mock_sound.assert_called_once()
        args, kwargs = mock_sound.call_args
        assert kwargs.get("dry_run") is True
        # No DB alert rows written for siren in dry_run mode
        rows = _siren_rows(tmp_db)
        assert len(rows) == 0

    def test_siren_exception_does_not_block_telegram(self, tmp_db):
        """A crash in siren.sound must not prevent Telegram from being called."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        # System already in confirmed critical state so the siren path is exercised.
        db.set_state("last_status", "critical", conn=tmp_db)

        with patch("wlm.alerts.siren.sound", side_effect=RuntimeError("boom")), \
             patch("wlm.alerts.tg.send_message") as mock_tg:
            mock_tg.return_value = (True, None)
            # Should not raise
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_tg.assert_called_once()


class TestSirenDisabledIsSilent:
    def test_disabled_siren_writes_no_alert_row(self, tmp_db):
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_not_called()
        assert _siren_rows(tmp_db) == []
