"""Tests: siren mute/unmute logic in wlm.siren and process_alerts."""

from __future__ import annotations

from unittest.mock import patch, call

import pytest

from wlm import db, siren
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


# ---------------------------------------------------------------------------
# siren.is_muted / mute / unmute helpers
# ---------------------------------------------------------------------------

class TestSirenMuteHelpers:
    def test_is_muted_false_by_default(self, tmp_db):
        assert siren.is_muted(conn=tmp_db) is False

    def test_mute_sets_state(self, tmp_db):
        with patch("wlm.siren.publish") as mock_pub:
            mock_pub.return_value = (True, None)
            siren.mute(conn=tmp_db)
        muted_ts = db.get_state("siren_muted", default="", conn=tmp_db)
        assert muted_ts  # non-empty ISO timestamp
        assert siren.is_muted(conn=tmp_db) is True

    def test_mute_calls_stop_with_force(self, tmp_db):
        """mute() calls stop(force=True) to silence a currently sounding siren."""
        publish_calls = []

        def fake_publish(payload, conn=None, force=False):
            publish_calls.append((payload, force))
            return (True, None)

        with patch("wlm.siren.publish", side_effect=fake_publish):
            ok, err = siren.mute(conn=tmp_db)

        assert ok is True
        assert err is None
        # stop(force=True) → publish({"alarm": False}, force=True)
        assert len(publish_calls) == 1
        assert publish_calls[0] == ({"alarm": False}, True)

    def test_mute_sets_state_even_when_stop_fails(self, tmp_db):
        """The muted state is recorded even when the MQTT publish fails."""
        with patch("wlm.siren.publish") as mock_pub:
            mock_pub.return_value = (False, "MQTT connect failed")
            ok, err = siren.mute(conn=tmp_db)

        assert siren.is_muted(conn=tmp_db) is True
        assert ok is False
        assert err == "MQTT connect failed"

    def test_unmute_clears_state(self, tmp_db):
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=tmp_db)
        assert siren.is_muted(conn=tmp_db) is True
        siren.unmute(conn=tmp_db)
        assert siren.is_muted(conn=tmp_db) is False


# ---------------------------------------------------------------------------
# _sound_siren: muted path
# ---------------------------------------------------------------------------

class TestSoundSirenMuted:
    def test_muted_no_publish(self, tmp_db):
        """When muted, _sound_siren must not call siren.sound."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("siren_seconds", "60", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_not_called()

    def test_muted_inserts_suppressed_row(self, tmp_db):
        """When muted, a 'siren muted — not sounded' alert row is inserted."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=tmp_db)

        with patch("wlm.alerts.siren.sound"):
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        rows = _siren_rows(tmp_db)
        assert any("muted" in r["message"].lower() for r in rows)
        # delivered should be 0/False since siren was not sounded
        muted_row = next(r for r in rows if "muted" in r["message"].lower())
        assert muted_row["delivered"] == 0

    def test_muted_telegram_still_sent(self, tmp_db):
        """Telegram critical alert is still sent even when the siren is muted."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "1", conn=tmp_db)
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound, \
             patch("wlm.alerts.tg.send_message") as mock_tg:
            mock_tg.return_value = (True, None)
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        mock_sound.assert_not_called()
        mock_tg.assert_called_once()

    def test_muted_dry_run_no_row_no_publish(self, tmp_db):
        """In dry_run mode, mute state changes nothing and no row is written."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=tmp_db)

        with patch("wlm.alerts.siren.sound") as mock_sound:
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=True, conn=tmp_db)

        mock_sound.assert_not_called()
        assert _siren_rows(tmp_db) == []


# ---------------------------------------------------------------------------
# Auto-unmute in process_alerts
# ---------------------------------------------------------------------------

class TestAutoUnmute:
    def _setup_muted(self, conn):
        db.set_setting("siren_enabled", "1", conn=conn)
        db.set_setting("mqtt_host", "mqtt.local", conn=conn)
        db.set_setting("telegram_enabled", "0", conn=conn)
        db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=conn)

    def test_normal_reading_unmutes(self, tmp_db):
        self._setup_muted(tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)

        process_alerts("normal", _make_result("normal", 5.0), None, None,
                       dry_run=False, conn=tmp_db)

        assert siren.is_muted(conn=tmp_db) is False
        rows = _siren_rows(tmp_db)
        assert any("unmuted" in r["message"].lower() for r in rows)
        unmute_row = next(r for r in rows if "unmuted" in r["message"].lower())
        assert unmute_row["delivered"] == 1
        assert "normal" in unmute_row["message"].lower()

    def test_warning_reading_unmutes(self, tmp_db):
        self._setup_muted(tmp_db)
        db.set_setting("alert_on_warning", "0", conn=tmp_db)
        # last_status must be different from "warning" to enter that branch, but
        # auto-unmute runs independently — set last_status to critical.
        db.set_state("last_status", "critical", conn=tmp_db)

        process_alerts("warning", _make_result("warning", 55.0), None, None,
                       dry_run=False, conn=tmp_db)

        assert siren.is_muted(conn=tmp_db) is False
        rows = _siren_rows(tmp_db)
        assert any("unmuted" in r["message"].lower() for r in rows)
        unmute_row = next(r for r in rows if "unmuted" in r["message"].lower())
        assert "warning" in unmute_row["message"].lower()

    def test_critical_keeps_mute(self, tmp_db):
        self._setup_muted(tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)

        with patch("wlm.alerts.siren.sound"):
            process_alerts("critical", _make_result("critical", 95.0), None, None,
                           dry_run=False, conn=tmp_db)

        assert siren.is_muted(conn=tmp_db) is True

    def test_unknown_keeps_mute(self, tmp_db):
        self._setup_muted(tmp_db)
        db.set_state("last_status", "normal", conn=tmp_db)

        process_alerts("unknown", _make_result("unknown"), None, None,
                       dry_run=False, conn=tmp_db)

        assert siren.is_muted(conn=tmp_db) is True

    def test_dry_run_does_not_unmute(self, tmp_db):
        """dry_run must not change the muted state."""
        self._setup_muted(tmp_db)
        db.set_state("last_status", "critical", conn=tmp_db)

        process_alerts("normal", _make_result("normal", 5.0), None, None,
                       dry_run=True, conn=tmp_db)

        # State unchanged in dry_run
        assert siren.is_muted(conn=tmp_db) is True
        # No row written
        assert _siren_rows(tmp_db) == []

    def test_not_muted_normal_does_not_write_unmute_row(self, tmp_db):
        """When not muted, normal reading must not insert a spurious unmute row."""
        db.set_setting("siren_enabled", "1", conn=tmp_db)
        db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        db.set_state("last_status", "warning", conn=tmp_db)
        # siren_muted is NOT set

        process_alerts("normal", _make_result("normal", 5.0), None, None,
                       dry_run=False, conn=tmp_db)

        rows = _siren_rows(tmp_db)
        assert not any("unmuted" in r["message"].lower() for r in rows)


class TestMutedLevel:
    """The mute lifts only when the water drops below the level it was muted at."""

    def _mute_at(self, conn, last_status):
        db.set_state("last_status", last_status, conn=conn)
        with patch("wlm.siren.stop", return_value=(True, None)):
            siren.mute(conn=conn)

    def test_mute_records_current_level(self, tmp_db):
        self._mute_at(tmp_db, "warning")
        assert siren.muted_level(conn=tmp_db) == "warning"

    def test_mute_at_normal_is_treated_as_critical(self, tmp_db):
        self._mute_at(tmp_db, "normal")
        assert siren.muted_level(conn=tmp_db) == "critical"

    def test_muted_at_warning_stays_muted_on_warning(self, tmp_db):
        self._mute_at(tmp_db, "warning")
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        process_alerts("warning", _make_result("warning", 55.0), None, None,
                       dry_run=False, conn=tmp_db)
        assert siren.is_muted(conn=tmp_db) is True

    def test_muted_at_warning_unmutes_on_normal(self, tmp_db):
        self._mute_at(tmp_db, "warning")
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        process_alerts("normal", _make_result("normal", 10.0), None, None,
                       dry_run=False, conn=tmp_db)
        assert siren.is_muted(conn=tmp_db) is False
        assert db.get_state("siren_muted_level", default="", conn=tmp_db) == ""

    def test_muted_at_critical_unmutes_on_warning(self, tmp_db):
        self._mute_at(tmp_db, "critical")
        db.set_setting("telegram_enabled", "0", conn=tmp_db)
        process_alerts("warning", _make_result("warning", 55.0), None, None,
                       dry_run=False, conn=tmp_db)
        assert siren.is_muted(conn=tmp_db) is False
        msgs = [r["message"] for r in _siren_rows(tmp_db)]
        assert any("below CRITICAL (warning)" in m for m in msgs)
