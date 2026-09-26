"""MQTT siren control for the water-level monitor.

Publishes alarm payloads to a zigbee2mqtt topic (default: zigbee2mqtt/Siren/set).
All network calls are best-effort: failures are returned as (False, error_str)
and never raise.  The password is never included in any error string.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("wlm.siren")

# Lazy import so tests can patch before the real module is loaded.
def _mqtt():
    import paho.mqtt.client as mqtt  # noqa: PLC0415
    return mqtt


def _conn_cfg(conn=None):
    """Return (host, port, username, password, topic) from settings."""
    from wlm import settings  # local import to avoid circular
    host = settings.get("mqtt_host", conn=conn).strip()
    port = settings.get_int("mqtt_port", conn=conn)
    username = settings.get("mqtt_username", conn=conn).strip()
    password = settings.get("mqtt_password", conn=conn)
    topic = settings.get("siren_topic", conn=conn).strip() or "zigbee2mqtt/Siren/set"
    return host, port, username, password, topic


def _is_enabled(conn=None) -> bool:
    from wlm import settings
    return settings.get_bool("siren_enabled", conn=conn)


def is_configured(conn=None) -> bool:
    """True when the siren is enabled and an MQTT host is set."""
    if not _is_enabled(conn=conn):
        return False
    return bool(_conn_cfg(conn=conn)[0])


def publish(payload: dict, conn=None, force: bool = False) -> tuple[bool, str | None]:
    """Connect to the MQTT broker, publish *payload* as JSON with QoS 1, disconnect.

    Returns (True, None) on success, (False, error_message) on failure.
    The error message never contains the MQTT password.  *force* skips the
    siren_enabled check (used by the dashboard / CLI test so the siren can be
    tested before it is enabled); the host must still be set.
    """
    if not force and not _is_enabled(conn=conn):
        return False, "siren disabled/not configured"

    host, port, username, password, topic = _conn_cfg(conn=conn)
    if not host:
        return False, "siren disabled/not configured"

    import json
    import time
    mqtt = _mqtt()

    client_id = "wlm-siren"
    connect_reason: list[str] = []

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        connect_reason.append(str(reason_code))

    client = None
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        client.on_connect = _on_connect
        if username:
            client.username_pw_set(username, password)
        client.connect(host, port, keepalive=10)
        client.loop_start()
        # A refused login (e.g. wrong password) only shows up in CONNACK, so wait
        # for the connection before publishing instead of timing out on the publish.
        deadline = time.monotonic() + 5.0
        while not client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not client.is_connected():
            reason = connect_reason[0] if connect_reason else "no answer from broker"
            return False, f"MQTT connect failed: {reason}"
        info = client.publish(topic, json.dumps(payload), qos=1)
        # paho-mqtt 2.x wait_for_publish() returns None; is_published() says whether
        # the broker acknowledged the QoS 1 message.
        info.wait_for_publish(timeout=5.0)
        if not info.is_published():
            return False, "publish timed out"
        return True, None
    except Exception as exc:
        msg = str(exc)
        # Scrub password from the error string
        if password and password in msg:
            msg = msg.replace(password, "***")
        logger.warning("siren publish error: %s", msg)
        return False, msg
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass


def sound(seconds: int, conn=None, dry_run: bool = False) -> tuple[bool, str | None]:
    """Turn the siren on and schedule auto-off after *seconds* seconds.

    Sets runtime_state key siren_off_at to the UTC ISO timestamp of when the
    siren should be silenced.  In dry_run mode prints only — no publish, no
    state change.
    """
    if dry_run:
        print(f"[DRY RUN] siren: ON for {seconds} s")
        return True, None

    ok, err = publish({"alarm": True}, conn=conn)
    # Schedule the auto-off even when ON reports failure: the message may still have
    # reached the siren, and stop() is harmless when it is already silent.
    from wlm import db
    off_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )
    db.set_state("siren_off_at", off_at, conn=conn)
    logger.info("Siren ON (ok=%s) — will stop at %s", ok, off_at)
    return ok, err


def stop(conn=None, force: bool = False) -> tuple[bool, str | None]:
    """Publish alarm=false and clear the siren_off_at state.

    *force* sends OFF even when the siren is disabled (manual stop button).
    """
    ok, err = publish({"alarm": False}, conn=conn, force=force)
    if ok:
        from wlm import db
        db.set_state("siren_off_at", "", conn=conn)
        logger.info("Siren OFF")
    return ok, err


def maybe_stop_due(conn=None) -> None:
    """If a scheduled siren_off_at is set and in the past, call stop()."""
    from wlm import db
    off_at_str = db.get_state("siren_off_at", default="", conn=conn) or ""
    if not off_at_str:
        return
    try:
        off_at = datetime.fromisoformat(off_at_str)
        if off_at.tzinfo is None:
            off_at = off_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return
    now = datetime.now(timezone.utc)
    if now >= off_at:
        logger.info("Siren off time reached (%s) — stopping siren", off_at_str)
        stop(conn=conn)


def is_muted(conn=None) -> bool:
    """True when the siren is muted (runtime_state key siren_muted is set)."""
    from wlm import db
    return bool(db.get_state("siren_muted", default="", conn=conn))


_STATUS_RANK = {"normal": 0, "warning": 1, "critical": 2}


def muted_level(conn=None) -> str:
    """Status the mute was taken at: it lifts once a reading drops below this level."""
    from wlm import db
    level = db.get_state("siren_muted_level", default="", conn=conn) or "critical"
    return level if level in _STATUS_RANK else "critical"


def should_unmute(final_status: str, conn=None) -> bool:
    """True when *final_status* is below the level the siren was muted at."""
    if final_status not in _STATUS_RANK:
        return False  # unknown keeps the mute
    return _STATUS_RANK[final_status] < _STATUS_RANK[muted_level(conn=conn)]


def mute(conn=None) -> tuple[bool, str | None]:
    """Mute the siren: set siren_muted timestamp, then silence any sounding siren.

    The mute lifts when a reading drops below the current level (WARNING or
    CRITICAL; CRITICAL when the level is normal or unknown), so muting during a
    long WARNING is not undone by the next WARNING reading.

    Sets the siren_muted state key first so the mute is recorded even when the
    subsequent stop() call fails (e.g. MQTT broker unreachable).  Returns the
    result of stop(force=True) so callers can report publish errors.
    """
    from wlm import db
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last = db.get_state("last_status", default="", conn=conn) or ""
    db.set_state("siren_muted_level", last if last in ("warning", "critical") else "critical", conn=conn)
    db.set_state("siren_muted", ts, conn=conn)
    logger.info("Siren MUTED at %s", ts)
    ok, err = stop(conn=conn, force=True)
    return ok, err


def unmute(conn=None) -> None:
    """Clear the siren mute state so future alerts can sound the siren."""
    from wlm import db
    db.set_state("siren_muted", "", conn=conn)
    db.set_state("siren_muted_level", "", conn=conn)
    logger.info("Siren UNMUTED")
