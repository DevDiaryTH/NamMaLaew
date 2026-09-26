"""Tests for wlm.siren — all network I/O is mocked (paho is never actually imported)."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from wlm import db


# ---------------------------------------------------------------------------
# Helpers: build a minimal paho mock before wlm.siren is imported
# ---------------------------------------------------------------------------

def _make_paho_mock():
    """Return a MagicMock that looks like paho.mqtt.client."""
    mock_info = MagicMock()
    # Real paho-mqtt 2.x: wait_for_publish() returns None; is_published() reports the ack.
    mock_info.wait_for_publish.return_value = None
    mock_info.is_published.return_value = True

    mock_client_instance = MagicMock()
    mock_client_instance.publish.return_value = mock_info

    mock_mqtt = MagicMock()
    mock_mqtt.Client.return_value = mock_client_instance
    mock_mqtt.CallbackAPIVersion.VERSION2 = "VERSION2"

    # paho.mqtt.client is imported as a package attr
    mock_paho = types.ModuleType("paho")
    mock_paho_mqtt = types.ModuleType("paho.mqtt")
    mock_paho_mqtt.client = mock_mqtt
    mock_paho.mqtt = mock_paho_mqtt
    sys.modules["paho"] = mock_paho
    sys.modules["paho.mqtt"] = mock_paho_mqtt
    sys.modules["paho.mqtt.client"] = mock_mqtt

    return mock_mqtt, mock_client_instance, mock_info


# Pre-install the mock before wlm.siren is loaded so the lazy import never
# touches the real paho.
_MOCK_MQTT, _MOCK_CLIENT_INSTANCE, _MOCK_INFO = _make_paho_mock()


# Now it is safe to import
from wlm import siren  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_paho(tmp_db):
    """Reset paho mocks before each test; also configure siren as enabled."""
    _MOCK_CLIENT_INSTANCE.reset_mock()
    _MOCK_INFO.wait_for_publish.return_value = None
    _MOCK_INFO.is_published.return_value = True
    _MOCK_CLIENT_INSTANCE.is_connected.return_value = True
    _MOCK_CLIENT_INSTANCE.connect.side_effect = None
    db.set_setting("siren_enabled", "1", conn=tmp_db)
    db.set_setting("mqtt_host", "mqtt.local", conn=tmp_db)
    db.set_setting("mqtt_port", "1883", conn=tmp_db)
    db.set_setting("mqtt_username", "user", conn=tmp_db)
    db.set_setting("mqtt_password", "s3cr3t", conn=tmp_db)
    db.set_setting("siren_topic", "zigbee2mqtt/Siren/set", conn=tmp_db)
    db.set_setting("siren_seconds", "60", conn=tmp_db)
    yield


# ---------------------------------------------------------------------------
# publish() — success
# ---------------------------------------------------------------------------

class TestPublish:
    def test_success(self, tmp_db):
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        assert ok is True
        assert err is None
        _MOCK_CLIENT_INSTANCE.publish.assert_called_once()

    def test_publish_payload_is_json(self, tmp_db):
        import json
        siren.publish({"alarm": False, "volume": 80}, conn=tmp_db)
        args, kwargs = _MOCK_CLIENT_INSTANCE.publish.call_args
        payload_str = args[1]
        parsed = json.loads(payload_str)
        assert parsed == {"alarm": False, "volume": 80}

    def test_uses_username_password(self, tmp_db):
        siren.publish({"alarm": True}, conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.username_pw_set.assert_called_once_with("user", "s3cr3t")

    def test_success_when_wait_for_publish_returns_none(self, tmp_db):
        # Regression: paho 2.x returns None here even when the broker acknowledged.
        _MOCK_INFO.wait_for_publish.return_value = None
        _MOCK_INFO.is_published.return_value = True
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        assert ok is True and err is None

    def test_timeout_returns_false(self, tmp_db):
        _MOCK_INFO.is_published.return_value = False
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        assert ok is False
        assert err is not None

    def test_exception_returns_false(self, tmp_db):
        _MOCK_CLIENT_INSTANCE.connect.side_effect = ConnectionRefusedError("refused")
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.connect.side_effect = None
        assert ok is False
        assert err is not None

    def test_password_never_in_error(self, tmp_db):
        _MOCK_CLIENT_INSTANCE.connect.side_effect = Exception("bad pass s3cr3t here")
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.connect.side_effect = None
        assert ok is False
        assert "s3cr3t" not in (err or "")

    def test_disabled_returns_false_no_connect(self, tmp_db):
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.reset_mock()
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        assert ok is False
        assert "disabled" in (err or "")
        _MOCK_CLIENT_INSTANCE.connect.assert_not_called()

    def test_no_host_returns_false_no_connect(self, tmp_db):
        db.set_setting("mqtt_host", "", conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.reset_mock()
        ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        assert ok is False
        _MOCK_CLIENT_INSTANCE.connect.assert_not_called()


# ---------------------------------------------------------------------------
# sound() — sets siren_off_at
# ---------------------------------------------------------------------------

class TestSound:
    def test_sound_sets_siren_off_at(self, tmp_db):
        before = datetime.now(timezone.utc)
        siren.sound(60, conn=tmp_db)
        off_at_str = db.get_state("siren_off_at", conn=tmp_db)
        assert off_at_str, "siren_off_at should be set"
        off_at = datetime.fromisoformat(off_at_str)
        if off_at.tzinfo is None:
            off_at = off_at.replace(tzinfo=timezone.utc)
        assert off_at > before + timedelta(seconds=55)
        assert off_at < before + timedelta(seconds=65)

    def test_sound_dry_run_does_not_publish(self, tmp_db):
        _MOCK_CLIENT_INSTANCE.reset_mock()
        siren.sound(60, conn=tmp_db, dry_run=True)
        _MOCK_CLIENT_INSTANCE.connect.assert_not_called()
        off_at_str = db.get_state("siren_off_at", conn=tmp_db)
        assert not off_at_str

    def test_sound_publishes_alarm_true(self, tmp_db):
        import json
        siren.sound(60, conn=tmp_db)
        args, _ = _MOCK_CLIENT_INSTANCE.publish.call_args
        assert json.loads(args[1]) == {"alarm": True}


# ---------------------------------------------------------------------------
# stop() — clears siren_off_at
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_clears_siren_off_at(self, tmp_db):
        db.set_state("siren_off_at", "2099-01-01T00:00:00+00:00", conn=tmp_db)
        siren.stop(conn=tmp_db)
        off_at_str = db.get_state("siren_off_at", conn=tmp_db)
        assert not off_at_str

    def test_stop_publishes_alarm_false(self, tmp_db):
        import json
        siren.stop(conn=tmp_db)
        args, _ = _MOCK_CLIENT_INSTANCE.publish.call_args
        assert json.loads(args[1]) == {"alarm": False}


# ---------------------------------------------------------------------------
# maybe_stop_due()
# ---------------------------------------------------------------------------

class TestMaybeStopDue:
    def test_stops_when_due(self, tmp_db):
        # Set off_at to 5 s ago
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        db.set_state("siren_off_at", past, conn=tmp_db)
        siren.maybe_stop_due(conn=tmp_db)
        # stop() should have published alarm=false and cleared state
        import json
        args, _ = _MOCK_CLIENT_INSTANCE.publish.call_args
        assert json.loads(args[1]) == {"alarm": False}
        assert not db.get_state("siren_off_at", conn=tmp_db)

    def test_does_not_stop_when_not_due(self, tmp_db):
        # Set off_at to 60 s in the future
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
        db.set_state("siren_off_at", future, conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.reset_mock()
        siren.maybe_stop_due(conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.publish.assert_not_called()

    def test_does_nothing_when_state_empty(self, tmp_db):
        _MOCK_CLIENT_INSTANCE.reset_mock()
        siren.maybe_stop_due(conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.publish.assert_not_called()


class TestConnectAndForce:
    def test_refused_login_reports_reason(self, tmp_db):
        _MOCK_CLIENT_INSTANCE.is_connected.return_value = False

        def fake_connect(*a, **k):
            cb = _MOCK_CLIENT_INSTANCE.on_connect
            cb(_MOCK_CLIENT_INSTANCE, None, None, "Not authorized", None)

        _MOCK_CLIENT_INSTANCE.connect.side_effect = fake_connect
        with patch("time.monotonic", side_effect=[0.0, 10.0, 10.0]):
            ok, err = siren.publish({"alarm": True}, conn=tmp_db)
        _MOCK_CLIENT_INSTANCE.connect.side_effect = None
        _MOCK_CLIENT_INSTANCE.is_connected.return_value = True
        assert ok is False
        assert "Not authorized" in err
        _MOCK_CLIENT_INSTANCE.publish.assert_not_called()

    def test_force_bypasses_disabled(self, tmp_db):
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        ok, err = siren.publish({"alarm": True}, conn=tmp_db, force=True)
        assert ok is True

    def test_force_still_needs_host(self, tmp_db):
        db.set_setting("mqtt_host", "", conn=tmp_db)
        ok, err = siren.publish({"alarm": True}, conn=tmp_db, force=True)
        assert ok is False

    def test_is_configured(self, tmp_db):
        assert siren.is_configured(conn=tmp_db) is True
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        assert siren.is_configured(conn=tmp_db) is False


class TestSoundAlwaysSchedulesOff:
    def test_off_scheduled_even_when_on_reports_failure(self, tmp_db):
        _MOCK_INFO.is_published.return_value = False
        ok, err = siren.sound(60, conn=tmp_db)
        assert ok is False
        assert db.get_state("siren_off_at", default="", conn=tmp_db)


class TestStopForce:
    def test_stop_force_sends_when_disabled(self, tmp_db):
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        db.set_state("siren_off_at", "2099-01-01T00:00:00+00:00", conn=tmp_db)
        ok, err = siren.stop(conn=tmp_db, force=True)
        assert ok is True
        assert db.get_state("siren_off_at", default="", conn=tmp_db) == ""

    def test_stop_without_force_respects_disabled(self, tmp_db):
        db.set_setting("siren_enabled", "0", conn=tmp_db)
        ok, err = siren.stop(conn=tmp_db)
        assert ok is False
