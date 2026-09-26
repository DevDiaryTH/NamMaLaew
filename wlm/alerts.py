"""Alert policy: decide when to alert, send via Telegram, record in DB.

State keys in runtime_state:
  last_status            - last non-unknown status
  consecutive_failures   - int, count of consecutive unknown readings
  failure_alert_sent     - "1" | "0"
  last_critical_alert_ts - ISO timestamp of last critical alert (or "")
  last_heartbeat_date    - YYYY-MM-DD string or ""
  critical_pending       - ISO UTC timestamp of the first unconfirmed CRITICAL reading,
                           or "" when no confirmation is pending.  Set only when
                           critical_confirm is on.  last_status is NOT updated to
                           "critical" while a confirmation is pending, so that a
                           warning→pending→recovery sequence still fires a recovery alert.
  recheck_at             - ISO UTC timestamp after which the runner should run a cycle
                           immediately (to re-check an unconfirmed critical).  "" when
                           no recheck is scheduled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from wlm import db, settings, siren
from wlm import telegram as tg

logger = logging.getLogger("wlm.alerts")


def _get_state_int(key: str, conn=None) -> int:
    val = db.get_state(key, default="0", conn=conn)
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _get_state_float(key: str, conn=None) -> float | None:
    val = db.get_state(key, default="", conn=conn)
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _derive_final_status(
    model_status: str | None,
    level_index: float | None,
    per_lens: list[dict] | None = None,
    conn=None,
) -> str:
    """Derive the final status from model output + threshold settings.

    Escalation rules (applied only when model_status is not 'unknown'):
    - If any lens line_position == 'at_or_above_critical' → at least critical
    - If any lens line_position == 'at_or_above_warning'  → at least warning
    """
    if model_status is None or model_status == "unknown" or level_index is None:
        return "unknown"
    level_critical = settings.get_float("level_critical", conn=conn)
    level_warning = settings.get_float("level_warning", conn=conn)
    if level_index >= level_critical:
        status = "critical"
    elif level_index >= level_warning:
        status = "warning"
    else:
        status = "normal"

    # Escalate from line positions
    if per_lens:
        for lens in per_lens:
            lp = lens.get("line_position")
            if lp == "at_or_above_critical":
                return "critical"
        for lens in per_lens:
            lp = lens.get("line_position")
            if lp == "at_or_above_warning" and status == "normal":
                status = "warning"

    return status


def derive_final_status(
    model_status: str | None,
    level_index: float | None,
    per_lens: list[dict] | None = None,
    conn=None,
) -> str:
    """Public wrapper — used by runner and tests."""
    return _derive_final_status(model_status, level_index, per_lens=per_lens, conn=conn)


def _send_alert(
    kind: str,
    message: str,
    photo_path: Path | None,
    reading_id: int | None,
    dry_run: bool,
    conn=None,
) -> None:
    """Send a Telegram alert and record it in the DB."""
    token = settings.get("telegram_bot_token", conn=conn)
    chat_id = settings.get("telegram_chat_id", conn=conn)
    telegram_enabled = settings.get_bool("telegram_enabled", conn=conn)

    if dry_run:
        print(f"[DRY RUN] alert kind={kind}:\n{message}\n" + "-" * 40)
        return

    if not telegram_enabled or not token or not chat_id:
        error_msg = "telegram disabled/not configured"
        logger.info("Skipping alert (%s): %s", kind, error_msg)
        db.insert_alert(kind, message, delivered=False, error=error_msg,
                        reading_id=reading_id, conn=conn)
        return

    if photo_path and photo_path.exists():
        ok, err = tg.send_photo(token, chat_id, photo_path, message)
    else:
        ok, err = tg.send_message(token, chat_id, message)

    db.insert_alert(kind, message, delivered=ok, error=err,
                    reading_id=reading_id, conn=conn)


def _sound_siren(reason: str, reading_id: int | None, dry_run: bool, conn=None) -> None:
    """Sound the MQTT siren and log it; never raises, silent when the siren is off."""
    try:
        if not siren.is_configured(conn=conn):
            return
        seconds = settings.get_int("siren_seconds", conn=conn)
        ok, err = siren.sound(seconds, conn=conn, dry_run=dry_run)
        if not dry_run:
            db.insert_alert("siren", f"Siren ON {seconds} s ({reason})", delivered=ok,
                            error=err, reading_id=reading_id, conn=conn)
    except Exception as siren_exc:
        logger.warning("Siren sound error: %s", siren_exc)


def process_alerts(
    final_status: str,
    result: dict,
    reading_id: int | None,
    photo_path: Path | None,
    dry_run: bool = False,
    conn=None,
) -> None:
    """Apply alert policy, send notifications, update runtime_state.

    unknown does NOT overwrite last_status so that warning→unknown→normal
    still fires a recovery alert.

    When critical_confirm is on, the first CRITICAL reading enters a pending
    state (no Telegram, no siren) and schedules a re-check via recheck_at.
    Only a second consecutive CRITICAL sounds the siren and sends the alert.
    last_status is never set to "critical" for a pending (unconfirmed) reading.
    """
    ts = _now_ts()
    now = datetime.now(timezone.utc)

    last_status = db.get_state("last_status", default="", conn=conn) or None
    consecutive = _get_state_int("consecutive_failures", conn=conn)
    failure_alert_sent = db.get_state("failure_alert_sent", default="0", conn=conn) == "1"
    last_critical_ts_str = db.get_state("last_critical_alert_ts", default="", conn=conn) or ""
    # critical_pending: ISO UTC ts of first unconfirmed critical, "" when none pending
    critical_pending = db.get_state("critical_pending", default="", conn=conn) or ""

    desc = result.get("estimated_level_description", "")
    reason = result.get("reason", "")
    level_index = result.get("level_index")
    level_index_str = f"{level_index:.1f}" if level_index is not None else "unknown"
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # This flag prevents last_status from being set to "critical" when the reading
    # enters pending (unconfirmed) state.
    _skip_last_status_update = False

    # ---- cancel pending critical when re-check is not critical ----
    # Must happen before the main status branches so warning/recovery logic
    # operates against the un-promoted last_status.
    if final_status != "critical" and critical_pending:
        msg = f"CRITICAL not confirmed (re-check: {final_status})"
        if dry_run:
            print(f"[DRY RUN] {msg}")
        else:
            db.insert_alert("pending", msg, delivered=False, error=None,
                            reading_id=reading_id, conn=conn)
            db.set_state("critical_pending", "", conn=conn)
        logger.info("Critical pending cleared — re-check result: %s", final_status)

    # ---- critical alert ----
    if final_status == "critical":
        critical_confirm = settings.get_bool("critical_confirm", conn=conn)

        # Enter pending when: confirm is on, no pending already, and last_status is not
        # already "critical" (i.e. this is a fresh transition into critical, not a
        # sustained critical state after a confirmed alert).
        if critical_confirm and not critical_pending and last_status != "critical":
            recheck_secs = settings.get_int("critical_confirm_recheck_seconds", conn=conn)
            recheck_ts = (now + timedelta(seconds=recheck_secs)).isoformat(timespec="seconds")
            pend_msg = (
                f"CRITICAL detected — re-checking in {recheck_secs} s before alerting"
            )
            if dry_run:
                print(f"[DRY RUN] critical pending: {pend_msg}")
                print(f"[DRY RUN] recheck_at would be set to {recheck_ts}")
            else:
                db.insert_alert("pending", pend_msg, delivered=False, error=None,
                                reading_id=reading_id, conn=conn)
                db.set_state("critical_pending", ts, conn=conn)
                db.set_state("recheck_at", recheck_ts, conn=conn)
            logger.info(
                "Critical pending (confirmation required) — recheck scheduled at %s",
                recheck_ts,
            )
            # Do not update last_status to "critical" for an unconfirmed reading.
            _skip_last_status_update = True

        else:
            # Either:
            #   a) critical_confirm is off → old behaviour
            #   b) critical_pending is set → this is the confirmation reading
            #   c) last_status is already "critical" → sustained critical, use repeat logic
            if critical_confirm and critical_pending:
                # Confirmed: clear the pending state
                if not dry_run:
                    db.set_state("critical_pending", "", conn=conn)
                logger.info("Critical confirmed — clearing pending state")

            critical_repeat = settings.get_int("critical_repeat_minutes", conn=conn)
            should_alert = True
            if last_critical_ts_str:
                try:
                    last_critical_ts = datetime.fromisoformat(last_critical_ts_str)
                    # Make timezone-aware if needed
                    if last_critical_ts.tzinfo is None:
                        last_critical_ts = last_critical_ts.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_critical_ts).total_seconds() / 60
                    if elapsed < critical_repeat:
                        should_alert = False
                        logger.debug(
                            "Suppressing critical repeat (%.1f min < %d min)",
                            elapsed, critical_repeat,
                        )
                except (ValueError, TypeError):
                    pass

            if should_alert:
                msg = (
                    f"🚨 น้ำระดับวิกฤต! (CRITICAL)\n"
                    f"level_index: {level_index_str}\n"
                    f"ระดับ: {desc}\n"
                    f"เหตุผล: {reason}\n"
                    f"เวลา: {now_local}"
                )
                _send_alert("critical", msg, photo_path, reading_id, dry_run, conn=conn)
                if not dry_run:
                    db.set_state("last_critical_alert_ts", ts, conn=conn)
                # ---- siren: critical ----
                _sound_siren("critical", reading_id, dry_run, conn=conn)

    # ---- warning alert (on transition) ----
    elif final_status == "warning" and last_status != "warning":
        if settings.get_bool("alert_on_warning", conn=conn):
            msg = (
                f"⚠️ น้ำระดับเตือนภัย! (WARNING)\n"
                f"level_index: {level_index_str}\n"
                f"ระดับ: {desc}\n"
                f"เหตุผล: {reason}\n"
                f"เวลา: {now_local}"
            )
            _send_alert("warning", msg, photo_path, reading_id, dry_run, conn=conn)
            # ---- siren: warning ----
            if settings.get_bool("siren_on_warning", conn=conn):
                _sound_siren("warning", reading_id, dry_run, conn=conn)

    # ---- recovery alert ----
    elif final_status == "normal" and last_status in ("warning", "critical"):
        if settings.get_bool("alert_on_recovery", conn=conn):
            msg = (
                f"✅ น้ำกลับสู่ระดับปกติ (RECOVERED)\n"
                f"level_index: {level_index_str}\n"
                f"ระดับ: {desc}\n"
                f"เวลา: {now_local}"
            )
            _send_alert("recovered", msg, None, reading_id, dry_run, conn=conn)
        # ---- siren: stop on recovery ----
        siren_off_at = db.get_state("siren_off_at", default="", conn=conn) or ""
        if siren_off_at and not dry_run:
            try:
                ok, err = siren.stop(conn=conn)
                db.insert_alert("siren", "Siren OFF", delivered=ok, error=err,
                                reading_id=reading_id, conn=conn)
            except Exception as siren_exc:
                logger.warning("Siren stop error: %s", siren_exc)

    # ---- consecutive failure alert ----
    if final_status == "unknown":
        new_consecutive = consecutive + 1
    else:
        new_consecutive = 0

    failure_threshold = settings.get_int("failure_threshold", conn=conn)
    failure_newly_sent = False
    if new_consecutive >= failure_threshold and not failure_alert_sent:
        if settings.get_bool("alert_on_failure", conn=conn):
            msg = (
                f"📷 กล้อง/การวิเคราะห์ล้มเหลว {new_consecutive} ครั้งติดต่อกัน\n"
                f"(Camera/analysis failing {new_consecutive} times in a row)\n"
                f"เวลา: {now_local}"
            )
            _send_alert("failure", msg, None, reading_id, dry_run, conn=conn)
            failure_newly_sent = True

    # ---- update state (skip in dry_run) ----
    if dry_run:
        return

    # unknown does NOT overwrite last_status.
    # A pending (unconfirmed) critical also must not overwrite last_status, so that
    # a later warning→recovery sequence fires correctly.
    if final_status != "unknown" and not _skip_last_status_update:
        db.set_state("last_status", final_status, conn=conn)

    db.set_state("consecutive_failures", str(new_consecutive), conn=conn)
    if new_consecutive == 0:
        db.set_state("failure_alert_sent", "0", conn=conn)
    elif failure_newly_sent:
        db.set_state("failure_alert_sent", "1", conn=conn)


def maybe_send_heartbeat(
    dry_run: bool = False,
    conn=None,
) -> None:
    """Send a once-per-day heartbeat message at heartbeat_hour (local time)."""
    hour_str = settings.get("heartbeat_hour", conn=conn).strip()
    if not hour_str:
        return
    try:
        heartbeat_hour = int(hour_str)
    except ValueError:
        logger.warning("heartbeat_hour is not a valid integer: %r", hour_str)
        return

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    last_hb = db.get_state("last_heartbeat_date", default="", conn=conn) or ""
    if now.hour != heartbeat_hour or last_hb == today:
        return

    msg = (
        f"💧 NamMaLaew / น้ำมาแล้ว! — ระบบทำงานปกติ\n"
        f"เวลา: {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    if dry_run:
        print(f"[DRY RUN] heartbeat: {msg}")
        return

    token = settings.get("telegram_bot_token", conn=conn)
    chat_id = settings.get("telegram_chat_id", conn=conn)
    telegram_enabled = settings.get_bool("telegram_enabled", conn=conn)

    if telegram_enabled and token and chat_id:
        ok, err = tg.send_message(token, chat_id, msg)
        db.insert_alert("heartbeat", msg, delivered=ok, error=err, conn=conn)
    else:
        db.insert_alert("heartbeat", msg, delivered=False,
                        error="telegram disabled/not configured", conn=conn)

    db.set_state("last_heartbeat_date", today, conn=conn)
