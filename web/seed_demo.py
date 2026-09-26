"""Seed a demo database with ~3 days of realistic fake readings.

Usage:
    WLM_DB=/tmp/web-demo.db WLM_SNAPSHOT_DIR=/tmp/web-demo-snaps \
        python -m web.seed_demo

Generates:
- ~3 days of readings every 10 min (≈432 rows)
- A rising-flood event on day 2
- Some unknown/error readings scattered in
- Per-lens rows (street + carport)
- Weather rows (precipitation)
- Several alerts
- Small placeholder JPEG snapshots
"""

from __future__ import annotations

import os
import sys
import math
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow running as `python -m web.seed_demo` from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from wlm import db as wlm_db
from wlm import lines as wlm_lines


def _make_jpeg(path: Path, width: int = 160, height: int = 120, color: tuple = (40, 80, 120)) -> None:
    """Write a minimal placeholder JPEG using Pillow or raw bytes fallback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (width, height), color)
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), f"{color}", fill=(200, 200, 200))
        img.save(str(path), "JPEG", quality=30)
    except ImportError:
        # Minimal valid JPEG (1×1 pixel gray) as fallback
        MINIMAL_JPEG = bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD2, 0x8A, 0x28, 0x03, 0xFF, 0xD9,
        ])
        path.write_bytes(MINIMAL_JPEG)


def _level_to_line_position(level: float | None, is_unknown: bool,
                             level_warning: float = 50, level_critical: float = 90) -> str:
    """Map a level_index to a line_position value consistent with the demo lines."""
    if is_unknown:
        return "not_visible"
    if level is None:
        return "not_visible"
    if level >= level_critical:
        return "at_or_above_critical"
    elif level >= level_warning:
        return "at_or_above_warning"
    else:
        return "below_warning"


def _level_to_status(level: float, level_warning: float = 50, level_critical: float = 90) -> str:
    if level >= level_critical:
        return "critical"
    elif level >= level_warning:
        return "warning"
    else:
        return "normal"


def seed(conn=None):
    """Fill the DB with demo data."""
    if conn is None:
        conn = wlm_db.connect()

    snap_dir = wlm_db.snapshot_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)
    interval = timedelta(minutes=10)

    level_warning = 50.0
    level_critical = 90.0

    # Configure settings
    wlm_db.set_setting("capture_interval_minutes", "10", conn=conn)
    wlm_db.set_setting("level_warning", "50", conn=conn)
    wlm_db.set_setting("level_critical", "90", conn=conn)
    wlm_db.set_setting("telegram_enabled", "0", conn=conn)
    wlm_db.set_setting("claude_model", "sonnet", conn=conn)
    wlm_db.set_setting("telegram_bot_token", "demo_token_xxxx1234", conn=conn)
    wlm_db.set_setting("telegram_chat_id", "123456789", conn=conn)

    step = start
    reading_count = 0

    # Flood event: day 2, from 14:00 to 20:00 local
    flood_start = start + timedelta(days=1, hours=14)
    flood_peak = start + timedelta(days=1, hours=17)
    flood_end = start + timedelta(days=1, hours=22)

    last_status = "normal"
    prev_level = 10.0

    day_cache: dict[str, Path] = {}  # cache subdir per date

    while step < now:
        ts = step.isoformat(timespec="seconds")

        # Determine level based on flood profile
        hours_since_start = (step - start).total_seconds() / 3600
        day_of_period = (step - start).days

        # Baseline level with gentle noise
        base_level = 8.0 + 4 * math.sin(hours_since_start * 0.05) + random.uniform(-2, 2)

        # Flood ramp
        if flood_start <= step <= flood_end:
            t_flood = (step - flood_start).total_seconds()
            t_peak = (flood_peak - flood_start).total_seconds()
            t_total = (flood_end - flood_start).total_seconds()
            if step <= flood_peak:
                flood_level = 95 * (t_flood / t_peak)
            else:
                flood_level = 95 * (1 - (t_flood - t_peak) / (t_total - t_peak))
            flood_level = max(0, flood_level)
            base_level = base_level * 0.1 + flood_level * 0.9

        base_level = max(0, min(100, base_level))

        # Some unknown readings (~5%)
        is_unknown = random.random() < 0.05
        error_msg = None
        level_index = None
        confidence = None
        if is_unknown:
            status = "unknown"
            model_status = None
            error_msg = random.choice(["Analysis timeout", "Camera offline", "Low quality frame"])
        else:
            level_index = base_level + random.uniform(-1, 1)
            level_index = max(0, min(100, level_index))
            confidence = random.uniform(0.75, 0.99)
            status = _level_to_status(level_index, level_warning, level_critical)
            model_status = status

        # Snapshot paths
        date_str = step.strftime("%Y%m%d")
        time_str = step.strftime("%H%M%S")
        subdir = Path(date_str)
        composite_rel = str(subdir / f"{time_str}_composite.jpg")
        composite_abs = snap_dir / composite_rel

        # Write placeholder image (only create if not already exists)
        if not composite_abs.exists():
            r_int = int(base_level * 2) if not is_unknown else 80
            _make_jpeg(composite_abs, color=(max(0, 40 - r_int), 80, max(0, 120 - r_int // 2)))

        # Cost sim
        inp_tokens = random.randint(800, 1200) if not is_unknown else None
        out_tokens = random.randint(150, 350) if not is_unknown else None
        cost = round((inp_tokens or 0) * 0.000015 + (out_tokens or 0) * 0.000075, 6) if not is_unknown else None

        rid = wlm_db.insert_reading({
            "ts": ts,
            "status": status,
            "model_status": model_status,
            "level_index": level_index,
            "confidence": confidence,
            "description": f"Water level at {level_index:.0f}/100" if not is_unknown else None,
            "distance_to_critical": f"{max(0, level_critical - (level_index or 0)):.0f} units" if not is_unknown else None,
            "reason": _reason(status, level_index) if not is_unknown else None,
            "composite_path": composite_rel,
            "model": "claude-opus-5" if not is_unknown else None,
            "input_tokens": inp_tokens,
            "output_tokens": out_tokens,
            "cost_usd": cost,
            "capture_ms": random.randint(800, 2500),
            "analysis_ms": random.randint(3000, 8000) if not is_unknown else None,
            "error": error_msg,
        }, conn=conn)

        # Per-lens rows
        for lens_label, lens_color in [("street", (60, 60, 40)), ("carport", (40, 60, 80))]:
            lens_rel = str(subdir / f"{time_str}_{lens_label}.jpg")
            lens_abs = snap_dir / lens_rel
            if not lens_abs.exists():
                _make_jpeg(lens_abs, color=lens_color)

            lens_ok = not is_unknown and random.random() > 0.03
            line_pos = _level_to_line_position(level_index, is_unknown, level_warning, level_critical) if lens_ok else "not_visible"
            wlm_db.insert_lens_reading({
                "reading_id": rid,
                "label": lens_label,
                "snapshot_path": lens_rel if lens_ok else None,
                "ok": 1 if lens_ok else 0,
                "observation": _lens_observation(lens_label, level_index, status) if lens_ok and level_index else None,
                "water_coverage_pct": min(100, max(0, (level_index or 0) * (0.9 if lens_label == "street" else 0.7) + random.uniform(-3, 3))) if lens_ok and level_index else None,
                "brightness": random.uniform(40, 200) if lens_ok else None,
                "sharpness": random.uniform(20, 150) if lens_ok else None,
                "is_night": 1 if (step.hour < 6 or step.hour >= 22) else 0,
                "error": None if lens_ok else "Capture failed",
                "line_position": line_pos,
            }, conn=conn)

        # Alerts for status changes
        if status != last_status and not is_unknown:
            if status == "warning" and last_status == "normal":
                wlm_db.insert_alert("warning", f"Water level entered warning zone ({level_index:.0f}/100)", True, reading_id=rid, conn=conn)
            elif status == "critical" and last_status in ("normal", "warning"):
                wlm_db.insert_alert("critical", f"CRITICAL: Water level at {level_index:.0f}/100!", True, reading_id=rid, conn=conn)
            elif status == "normal" and last_status in ("warning", "critical"):
                wlm_db.insert_alert("recovered", "Water level returned to normal.", True, reading_id=rid, conn=conn)

        if not is_unknown:
            last_status = status

        reading_count += 1
        step += interval

    # Weather rows
    step = start
    while step < now:
        hour_ts = step.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        # More rain during flood event
        if flood_start <= step <= flood_end:
            precip = random.uniform(5, 25)
        else:
            precip = random.uniform(0, 3) if random.random() < 0.3 else 0.0
        wlm_db.upsert_weather(hour_ts, round(precip, 2), conn=conn)
        step += timedelta(hours=1)

    # Heartbeat alert
    wlm_db.insert_alert("heartbeat", "System alive. All sensors operational.", True, conn=conn)
    # Failure alert
    wlm_db.insert_alert("failure", "Camera offline for 3 consecutive readings.", False, error="Connection timeout", conn=conn)
    # Test alert
    wlm_db.insert_alert("test", "Dashboard test message", True, conn=conn)

    # Demo alert lines:
    # Street lens: warning line across image at y≈0.62 (gate bottom level),
    #              critical line at y≈0.30 (higher flood level).
    # Carport lens: warning line at y≈0.65, critical at y≈0.35.
    demo_lines = {
        "street": {
            "warning":  [[0.05, 0.62], [0.30, 0.60], [0.60, 0.62], [0.90, 0.63], [0.98, 0.61]],
            "critical": [[0.05, 0.30], [0.30, 0.28], [0.60, 0.30], [0.90, 0.31], [0.98, 0.29]],
        },
        "carport": {
            "warning":  [[0.05, 0.65], [0.50, 0.64], [0.95, 0.65]],
            "critical": [[0.05, 0.35], [0.50, 0.34], [0.95, 0.35]],
        },
    }
    try:
        wlm_lines.set_lines(demo_lines, conn=conn)
        print("Demo alert lines set.")
    except ValueError as e:
        print(f"Warning: could not set demo lines: {e}")

    conn.commit()
    print(f"Seeded {reading_count} readings into {wlm_db.db_path()}")
    print(f"Snapshots written to {snap_dir}")


def _reason(status: str, level: float | None) -> str:
    if level is None:
        return "Unable to determine water level."
    if status == "critical":
        return f"Water is at critical level ({level:.0f}/100). Immediate action may be required."
    elif status == "warning":
        return f"Water is rising and has reached the warning threshold ({level:.0f}/100)."
    else:
        return f"Water level is normal ({level:.0f}/100). No flooding detected."


def _lens_observation(label: str, level: float | None, status: str) -> str:
    if level is None:
        return "Unable to assess."
    if label == "street":
        if status == "critical":
            return "Road outside the gate is deeply flooded. Water covers the surface completely."
        elif status == "warning":
            return "Water on the street is rising. Approaching the gate line."
        else:
            return "Street appears dry or with minimal water. No flooding visible."
    else:
        if status == "critical":
            return "Driveway and carport are flooded. Water level is high, potentially reaching vehicle wheels."
        elif status == "warning":
            return "Water beginning to enter the driveway area through the gate."
        else:
            return "Carport and driveway are clear. No water intrusion detected."


if __name__ == "__main__":
    seed()
