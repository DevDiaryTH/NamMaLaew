"""Main orchestrator: run_cycle + CLI entry point.

Usage:
  python -m wlm.runner --once            (default)
  python -m wlm.runner --loop
  python -m wlm.runner --dry-run
  python -m wlm.runner --image street=path.jpg --image carport=path2.jpg
  python -m wlm.runner --snapshot-only
  python -m wlm.runner --test-telegram
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from wlm import alerts, capture, db, lines as wlm_lines, rain_model, settings, siren
from wlm.analysis import analyze_images
from wlm.overlay import draw_lines
from wlm.weather import refresh_weather

logger = logging.getLogger("wlm")

_LOG_DIR = Path("logs")


def _setup_logging() -> None:
    _LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "monitor.log", maxBytes=5 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)


def run_cycle(
    dry_run: bool = False,
    images: list[tuple[str, Path]] | None = None,
) -> int | None:
    """Execute one monitor cycle.

    Args:
        dry_run: if True, skip DB alert rows, state writes, and Telegram sends.
        images: list of (label, Path) to use instead of live capture.

    Returns:
        reading_id from the readings table, or None on catastrophic failure.
    """
    conn = db.connect()
    try:
        snap_dir = db.snapshot_dir()

        # ---- capture ----
        capture_start = time.monotonic()
        frames: list[tuple[str, Path]] = []
        failed_labels: list[str] = []
        lens_data: list[dict] = []  # per-lens info to store after analysis

        if images is not None:
            # Use provided images directly
            for label, path in images:
                if path.exists():
                    frames.append((label, path))
                    m = capture.image_metrics(path)
                    lens_data.append({
                        "label": label,
                        "snapshot_path": str(path.relative_to(snap_dir)) if path.is_relative_to(snap_dir) else str(path),
                        "ok": 1,
                        "brightness": m["brightness"],
                        "sharpness": m["sharpness"],
                        "is_night": 1 if m["is_night"] else 0,
                    })
                else:
                    failed_labels.append(label)
                    lens_data.append({
                        "label": label,
                        "snapshot_path": None,
                        "ok": 0,
                        "error": f"Path not found: {path}",
                    })
        else:
            streams = capture.parse_cam_streams()
            if not streams:
                logger.error("No camera streams configured. Set CAM_STREAMS or CAM_RTSP_URL.")
                return None

            for label, url in streams:
                path = capture.capture_snapshot(url, label)
                if path:
                    frames.append((label, path))
                    m = capture.image_metrics(path)
                    rel = str(path.relative_to(snap_dir)) if path.is_relative_to(snap_dir) else str(path)
                    lens_data.append({
                        "label": label,
                        "snapshot_path": rel,
                        "ok": 1,
                        "brightness": m["brightness"],
                        "sharpness": m["sharpness"],
                        "is_night": 1 if m["is_night"] else 0,
                    })
                else:
                    failed_labels.append(label)
                    lens_data.append({
                        "label": label,
                        "snapshot_path": None,
                        "ok": 0,
                        "error": "Capture failed",
                    })

        capture_ms = int((time.monotonic() - capture_start) * 1000)

        # ---- overlay: draw alert lines onto frames ----
        all_lines = wlm_lines.get_lines(conn=conn)
        # Build overlay frames (use overlay path when lines exist, raw path otherwise)
        overlay_frames: list[tuple[str, Path]] = []  # frames to send to Claude
        overlay_paths: dict[str, Path] = {}  # label -> overlay path (only when actually drawn)
        for label, path in frames:
            lens_lines = all_lines.get(label, {})
            if lens_lines:
                stem = path.stem
                dst = snap_dir / f"{stem}-lines.jpg"
                try:
                    draw_lines(path, lens_lines, dst)
                    overlay_frames.append((label, dst))
                    overlay_paths[label] = dst
                    logger.info("Overlay created for lens '%s': %s", label, dst.name)
                except Exception as exc:
                    logger.warning("Overlay failed for lens '%s': %s — using raw frame", label, exc)
                    overlay_frames.append((label, path))
            else:
                overlay_frames.append((label, path))

        # ---- composite (built from overlay frames so lines appear in alert photo) ----
        composite_path: Path | None = None
        composite_rel: str | None = None
        composite_frames = overlay_frames if overlay_frames else frames
        if len(composite_frames) > 1:
            try:
                composite_path = capture.build_composite(composite_frames)
                composite_rel = str(composite_path.relative_to(snap_dir)) if composite_path.is_relative_to(snap_dir) else str(composite_path)
            except Exception as exc:
                logger.warning("Failed to build composite: %s", exc)
                composite_path = composite_frames[0][1] if composite_frames else None
        elif composite_frames:
            composite_path = composite_frames[0][1]
            try:
                composite_rel = str(composite_path.relative_to(snap_dir))
            except ValueError:
                composite_rel = str(composite_path)

        # ---- analysis ----
        analysis_start = time.monotonic()
        model = settings.get("claude_model", conn=conn)

        # Determine if any captured lens looks like night mode
        any_night = any(bool(ld.get("is_night")) for ld in lens_data if ld.get("ok"))

        # Load verified reference examples for few-shot calibration
        from wlm import learning as wlm_learning
        try:
            examples = wlm_learning.select_examples(conn=conn, current_is_night=any_night)
        except Exception as exc:
            logger.warning("Failed to load learning examples: %s", exc)
            examples = []

        # Load recent-reading history context when the feature is enabled
        history_text: str | None = None
        if settings.get_bool("learning_use_history", conn=conn):
            try:
                history_text = wlm_learning.recent_context(conn=conn)
            except Exception as exc:
                logger.warning("Failed to load history context: %s", exc)

        if overlay_frames:
            result, input_tokens, output_tokens, cost_usd = analyze_images(
                overlay_frames, conn=conn, lens_lines=all_lines, examples=examples or None,
                history_text=history_text,
            )
        elif frames:
            result, input_tokens, output_tokens, cost_usd = analyze_images(
                frames, conn=conn, lens_lines=all_lines, examples=examples or None,
                history_text=history_text,
            )
        else:
            from wlm.analysis import _unknown_result
            result = _unknown_result("All snapshot captures failed")
            input_tokens, output_tokens, cost_usd = 0, 0, None

        analysis_ms = int((time.monotonic() - analysis_start) * 1000)

        model_status = result.get("level_status", "unknown")
        level_index = result.get("level_index")
        per_lens_result = result.get("per_lens", [])
        final_status = alerts.derive_final_status(
            model_status, level_index, per_lens=per_lens_result,
            lens_lines=all_lines, failed_labels=failed_labels, conn=conn,
        )
        failed_deciding = sorted(
            label for label in failed_labels if "critical" in all_lines.get(label, {})
        )

        logger.info(
            "Cycle done: final_status=%s model_status=%s level_index=%s",
            final_status, model_status, level_index,
        )

        # ---- store reading ----
        reading_values = {
            "status": final_status,
            "model_status": model_status,
            "level_index": level_index,
            "confidence": result.get("confidence"),
            "description": result.get("estimated_level_description"),
            "distance_to_critical": result.get("distance_to_critical"),
            "reason": result.get("reason"),
            "composite_path": composite_rel,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "capture_ms": capture_ms,
            "analysis_ms": analysis_ms,
        }
        if result.get("level_status") == "unknown" and result.get("reason"):
            reading_values["error"] = result["reason"]
        if failed_deciding:
            reading_values["error"] = (
                f"Capture failed for deciding lens: {', '.join(failed_deciding)}"
            )

        reading_id = db.insert_reading(reading_values, conn=conn)

        # ---- store per-lens readings ----
        per_lens_map = {pl["label"]: pl for pl in result.get("per_lens", [])}
        for ld in lens_data:
            label = ld["label"]
            pl = per_lens_map.get(label, {})
            # Validate line_position from analysis result
            raw_lp = pl.get("line_position")
            line_position = raw_lp if raw_lp in db.LINE_POSITIONS else None
            lens_row = {
                "reading_id": reading_id,
                "label": label,
                "snapshot_path": ld.get("snapshot_path"),
                "ok": ld["ok"],
                "observation": pl.get("observation"),
                "water_coverage_pct": pl.get("water_coverage_pct"),
                "brightness": ld.get("brightness"),
                "sharpness": ld.get("sharpness"),
                "is_night": ld.get("is_night"),
                "error": ld.get("error"),
                "line_position": line_position,
            }
            db.insert_lens_reading(lens_row, conn=conn)

        # ---- alerts ----
        alerts.process_alerts(
            final_status=final_status,
            result=result,
            reading_id=reading_id,
            photo_path=composite_path,
            dry_run=dry_run,
            conn=conn,
            # every lens failing is an unknown reading, already counted as a failure
            failed_labels=failed_labels if frames else None,
        )

        # ---- heartbeat ----
        alerts.maybe_send_heartbeat(dry_run=dry_run, conn=conn)

        # ---- weather ----
        try:
            refresh_weather(conn=conn)
        except Exception as exc:
            logger.warning("Weather refresh error: %s", exc)

        # ---- learned rain-to-water model ----
        try:
            rain_model.maybe_refit(conn)
            alerts.maybe_send_forecast_warning(
                rain_model.predict_rise(conn),
                level_warning=settings.get_float("level_warning", conn=conn),
                enabled=settings.get_bool("alert_on_forecast", conn=conn),
                dry_run=dry_run,
                conn=conn,
            )
        except Exception as exc:
            logger.warning("Rain model error: %s", exc)

        # ---- prune ----
        try:
            capture.prune_snapshots()
        except Exception as exc:
            logger.warning("Prune error: %s", exc)

        return reading_id

    except Exception as exc:
        logger.error("run_cycle error: %s", exc, exc_info=True)
        return None
    finally:
        conn.close()


def _should_run_now(last_run_ts: float, conn=None) -> bool:
    """Return True if it's time for a cycle: interval elapsed, run_now requested, or recheck due."""
    interval_minutes = settings.get_int("capture_interval_minutes", conn=conn)
    interval_seconds = interval_minutes * 60
    elapsed = time.monotonic() - last_run_ts

    if _run_now_requested(conn=conn):
        return True
    if _recheck_due(conn=conn):
        return True
    return elapsed >= interval_seconds


def _run_now_requested(conn=None) -> bool:
    """The dashboard stores the request time (ISO string); empty or "0" means no request."""
    flag = db.get_state("run_now_requested", default="", conn=conn) or ""
    return flag not in ("", "0")


def _clear_run_now(conn=None) -> None:
    db.set_state("run_now_requested", "", conn=conn)


def _recheck_due(conn=None) -> bool:
    """Return True if a critical re-check has been scheduled and the time has arrived."""
    recheck_at_str = db.get_state("recheck_at", default="", conn=conn) or ""
    if not recheck_at_str:
        return False
    try:
        recheck_at = datetime.fromisoformat(recheck_at_str)
        if recheck_at.tzinfo is None:
            recheck_at = recheck_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= recheck_at
    except (ValueError, TypeError):
        return False


def _clear_recheck_at(conn=None) -> None:
    db.set_state("recheck_at", "", conn=conn)


def _test_telegram() -> None:
    """Send a test Telegram message and exit."""
    from wlm import telegram as tg
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
        sys.exit(1)
    ok, err = tg.send_message(
        token, chat_id,
        f"✅ Water Monitor test — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    )
    sys.exit(0 if ok else 1)


def _test_siren() -> None:
    """Sound the siren for 1 s (on → sleep 1 → off) and exit."""
    logger.info("Testing siren: publishing alarm=true")
    ok_on, err_on = siren.publish({"alarm": True}, force=True)
    print("Siren ON — waiting 1 s" if ok_on else f"Siren ON failed: {err_on} — sending OFF anyway")
    time.sleep(1)
    # Always send OFF: a failed-looking ON may still have reached the siren.
    ok_off, err_off = siren.publish({"alarm": False}, force=True)
    if not ok_on or not ok_off:
        if not ok_off:
            logger.error("Siren OFF failed: %s", err_off)
            print(f"Siren OFF failed: {err_off}")
        sys.exit(1)
    print("Siren OFF — test complete")
    sys.exit(0)


def main() -> None:
    load_dotenv()
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="NamMaLaew water-level monitor — capture, analyze, alert."
    )
    parser.add_argument("--once", action="store_true", default=False,
                        help="Run one cycle and exit (default when no mode given)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously, cycling at capture_interval_minutes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Telegram sends and state writes; print what would be sent")
    parser.add_argument("--image", metavar="LABEL=PATH", action="append",
                        help="Use this image instead of live capture (repeatable)")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="Capture and print snapshot paths, then exit")
    parser.add_argument("--test-telegram", action="store_true",
                        help="Send a test Telegram message and exit")
    parser.add_argument("--test-siren", action="store_true",
                        help="Sound the siren for 1 s (publish on, sleep 1, publish off) and exit")
    args = parser.parse_args()

    if args.test_telegram:
        _test_telegram()
        return

    if args.test_siren:
        _test_siren()
        return

    # Parse --image LABEL=PATH args
    provided_images: list[tuple[str, Path]] | None = None
    if args.image:
        provided_images = []
        for spec in args.image:
            label, _sep, path_str = spec.partition("=")
            if not label or not path_str:
                logger.error("--image must be LABEL=PATH, got: %s", spec)
                sys.exit(1)
            provided_images.append((label.strip(), Path(path_str.strip())))

    if args.snapshot_only:
        streams = capture.parse_cam_streams()
        if not streams:
            logger.error("No camera streams configured.")
            sys.exit(1)
        any_ok = False
        for label, url in streams:
            path = capture.capture_snapshot(url, label)
            if path:
                print(str(path))
                any_ok = True
            else:
                logger.warning("Snapshot failed for lens '%s'", label)
        if not any_ok:
            logger.error("All snapshot captures failed")
            sys.exit(1)
        return

    if args.loop:
        logger.info("Starting monitor loop (tick every 15s)")
        last_run_ts: float = 0.0
        while True:
            try:
                tmp_conn = db.connect()
                should = _should_run_now(last_run_ts, conn=tmp_conn)
                if should:
                    if _run_now_requested(conn=tmp_conn):
                        _clear_run_now(conn=tmp_conn)
                    if _recheck_due(conn=tmp_conn):
                        _clear_recheck_at(conn=tmp_conn)
                tmp_conn.close()

                if should:
                    last_run_ts = time.monotonic()
                    try:
                        run_cycle(dry_run=args.dry_run, images=provided_images)
                    except Exception as exc:
                        logger.error("Loop cycle error: %s", exc, exc_info=True)

                # Check if siren auto-off time has been reached
                try:
                    siren.maybe_stop_due()
                except Exception as exc:
                    logger.warning("Siren maybe_stop_due error: %s", exc)
            except Exception as exc:
                logger.error("Loop tick error: %s", exc, exc_info=True)
            time.sleep(15)
    else:
        # --once (default)
        run_cycle(dry_run=args.dry_run, images=provided_images)


if __name__ == "__main__":
    main()
