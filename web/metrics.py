"""Pure metric functions for the WLM dashboard.

All functions accept a sqlite3.Connection so they can be unit-tested without
touching the real database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO UTC timestamp string to an aware datetime."""
    try:
        # Python 3.11+ fromisoformat handles +00:00
        dt = datetime.fromisoformat(ts)
    except ValueError:
        # Fallback: try stripping timezone info and assume UTC
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _range_to_timedelta(range_str: str) -> timedelta:
    mapping = {
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "3d": timedelta(days=3),
        "7d": timedelta(days=7),
    }
    return mapping.get(range_str, timedelta(hours=24))


def _cutoff_iso(range_str: str) -> str:
    dt = _now_utc() - _range_to_timedelta(range_str)
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Rise-rate / ETA
# ---------------------------------------------------------------------------

def count_window_points(conn: sqlite3.Connection, minutes: int = 60) -> int:
    """Non-null level readings in the same window compute_rise_rate uses."""
    cutoff = (_now_utc() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts >= ? AND level_index IS NOT NULL",
        (cutoff,),
    ).fetchone()[0]


def compute_rise_rate(conn: sqlite3.Connection, minutes: int = 60) -> Optional[dict]:
    """Return {"slope": float, "r2": float} or None.

    Returns None when:
    - fewer than 3 non-null readings exist in the window
    - slope < 1.0 units/hour (not meaningfully rising)
    - R² < 0.3 (noisy / unreliable regression fit)
    """
    cutoff = (_now_utc() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT ts, level_index FROM readings "
        "WHERE ts >= ? AND level_index IS NOT NULL "
        "ORDER BY ts ASC",
        (cutoff,),
    ).fetchall()
    if len(rows) < 3:
        return None

    # Convert ts to hours-since-epoch for regression
    xs = [_parse_ts(r["ts"]).timestamp() / 3600.0 for r in rows]
    ys = [r["level_index"] for r in rows]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom  # units per hour

    # Compute R²
    intercept = (sy - slope * sx) / n
    y_mean = sy / n
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    if ss_tot == 0:
        r2 = 1.0  # flat line: regression fits perfectly, slope will be near 0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - ss_res / ss_tot

    # Noise guard: only report as rising when the signal is meaningful
    if slope < 1.0:
        return None
    if r2 < 0.3:
        return None

    return {"slope": slope, "r2": r2}


def compute_eta_to_critical(
    conn: sqlite3.Connection,
    level_critical: float,
    slope: Optional[dict] = None,
) -> Optional[dict]:
    """Return ETA info dict or None if not rising.

    Returns dict with keys: slope, r2, eta_dt (datetime), hours, minutes, label
    Accepts slope as a dict {"slope": float, "r2": float} from compute_rise_rate,
    or None (in which case it calls compute_rise_rate internally).
    """
    if slope is None:
        slope = compute_rise_rate(conn)
    if slope is None:
        return None

    slope_val: float = slope["slope"] if isinstance(slope, dict) else float(slope)
    slope_r2: Optional[float] = slope.get("r2") if isinstance(slope, dict) else None

    if slope_val <= 0:
        return None

    # Get current level
    row = conn.execute(
        "SELECT level_index FROM readings WHERE level_index IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    current = row["level_index"]
    if current >= level_critical:
        return {
            "slope": slope_val,
            "r2": slope_r2,
            "eta_dt": None,
            "hours": 0,
            "minutes": 0,
            "label": "already critical",
        }

    hours_until = (level_critical - current) / slope_val
    eta_dt = _now_utc() + timedelta(hours=hours_until)
    h = int(hours_until)
    m = int((hours_until - h) * 60)
    return {
        "slope": slope_val,
        "r2": slope_r2,
        "eta_dt": eta_dt,
        "hours": h,
        "minutes": m,
        "label": f"in {h}h {m}m",
    }


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def is_stale(conn: sqlite3.Connection, capture_interval_minutes: int = 10) -> bool:
    """True if latest reading is older than 2× the capture interval."""
    row = conn.execute("SELECT ts FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return True
    age = _now_utc() - _parse_ts(row["ts"])
    return age > timedelta(minutes=2 * capture_interval_minutes)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def get_summary(conn: sqlite3.Connection) -> dict:
    """Return the latest reading plus computed fields."""
    row = conn.execute(
        "SELECT * FROM readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "unknown", "level_index": None, "ts": None, "stale": True}
    d = dict(row)

    # Per-lens rows for the latest reading
    lenses = conn.execute(
        "SELECT * FROM lens_readings WHERE reading_id = ?", (d["id"],)
    ).fetchall()
    d["lenses"] = [dict(lr) for lr in lenses]
    return d


# ---------------------------------------------------------------------------
# Series data
# ---------------------------------------------------------------------------

def get_series(conn: sqlite3.Connection, range_str: str = "24h") -> dict:
    """Return time-series data for charts."""
    cutoff = _cutoff_iso(range_str)

    readings = conn.execute(
        "SELECT r.id, r.ts, r.status, r.level_index, r.confidence "
        "FROM readings r WHERE r.ts >= ? ORDER BY r.ts ASC",
        (cutoff,),
    ).fetchall()

    reading_ids = [r["id"] for r in readings]
    lens_rows: dict[int, list] = {}
    if reading_ids:
        placeholders = ",".join("?" * len(reading_ids))
        lr = conn.execute(
            f"SELECT * FROM lens_readings WHERE reading_id IN ({placeholders})",
            reading_ids,
        ).fetchall()
        for row in lr:
            lens_rows.setdefault(row["reading_id"], []).append(dict(row))

    # Weather: precipitation for each hour in range
    weather = conn.execute(
        "SELECT hour_ts, precipitation_mm FROM weather WHERE hour_ts >= ? ORDER BY hour_ts ASC",
        (cutoff,),
    ).fetchall()

    return {
        "readings": [dict(r) for r in readings],
        "lens_rows": lens_rows,
        "weather": [dict(w) for w in weather],
    }


# ---------------------------------------------------------------------------
# Image quality series (brightness, sharpness per lens over time)
# ---------------------------------------------------------------------------

def get_image_series(conn: sqlite3.Connection, range_str: str = "24h") -> dict:
    """Return per-lens brightness/sharpness/is_night over the selected range.

    Returns:
    {
      "lenses": ["street", "carport"],
      "series": {
        "street": [{"ts": "...", "brightness": 120, "sharpness": 80, "is_night": 0}, ...],
        ...
      },
      "latest": {
        "street": {"brightness": 150, "sharpness": 90, "is_night": 0},
        ...
      }
    }
    """
    cutoff = _cutoff_iso(range_str)

    rows = conn.execute(
        """SELECT r.ts, lr.label, lr.brightness, lr.sharpness, lr.is_night
           FROM lens_readings lr
           JOIN readings r ON r.id = lr.reading_id
           WHERE r.ts >= ? AND lr.ok = 1 AND lr.brightness IS NOT NULL
           ORDER BY r.ts ASC, lr.label ASC""",
        (cutoff,),
    ).fetchall()

    series: dict[str, list] = {}
    for row in rows:
        label = row["label"]
        if label not in series:
            series[label] = []
        series[label].append({
            "ts": row["ts"],
            "brightness": row["brightness"],
            "sharpness": row["sharpness"],
            "is_night": row["is_night"],
        })

    # Get latest values per lens (not range-filtered)
    latest_rows = conn.execute(
        """SELECT lr.label, lr.brightness, lr.sharpness, lr.is_night
           FROM lens_readings lr
           JOIN readings r ON r.id = lr.reading_id
           WHERE lr.ok = 1 AND lr.brightness IS NOT NULL
           ORDER BY r.ts DESC""",
    ).fetchall()

    latest: dict[str, dict] = {}
    for row in latest_rows:
        label = row["label"]
        if label not in latest:
            latest[label] = {
                "brightness": row["brightness"],
                "sharpness": row["sharpness"],
                "is_night": row["is_night"],
            }

    lenses = sorted(series.keys())

    return {
        "lenses": lenses,
        "series": series,
        "latest": latest,
    }


# ---------------------------------------------------------------------------
# Analysis success rates / system health
# ---------------------------------------------------------------------------

def get_system_health(conn: sqlite3.Connection) -> dict:
    """Return 24h system health metrics."""
    cutoff = _cutoff_iso("24h")

    total = conn.execute(
        "SELECT COUNT(*) as n FROM readings WHERE ts >= ?", (cutoff,)
    ).fetchone()["n"]

    unknown = conn.execute(
        "SELECT COUNT(*) as n FROM readings WHERE ts >= ? AND status = 'unknown'",
        (cutoff,),
    ).fetchone()["n"]

    analysis_success = (total - unknown) / total if total > 0 else 0.0

    # Per-lens capture success
    lens_stats = conn.execute(
        """SELECT lr.label,
                  COUNT(*) as total,
                  SUM(lr.ok) as ok_count
           FROM lens_readings lr
           JOIN readings r ON r.id = lr.reading_id
           WHERE r.ts >= ?
           GROUP BY lr.label""",
        (cutoff,),
    ).fetchall()

    capture_rates = {}
    for row in lens_stats:
        capture_rates[row["label"]] = (
            row["ok_count"] / row["total"] if row["total"] > 0 else 0.0
        )

    avg_row = conn.execute(
        """SELECT AVG(capture_ms) as avg_cap,
                  AVG(analysis_ms) as avg_ana,
                  AVG(confidence) as avg_conf
           FROM readings WHERE ts >= ? AND status != 'unknown'""",
        (cutoff,),
    ).fetchone()

    return {
        "total_readings_24h": total,
        "analysis_success_rate": analysis_success,
        "capture_rates": capture_rates,
        "avg_capture_ms": avg_row["avg_cap"],
        "avg_analysis_ms": avg_row["avg_ana"],
        "avg_confidence": avg_row["avg_conf"],
    }


# ---------------------------------------------------------------------------
# Cost metrics
# ---------------------------------------------------------------------------

def get_cost_metrics(conn: sqlite3.Connection) -> dict:
    """Return daily cost for the last 7 days and 30-day projection."""
    rows = conn.execute(
        """SELECT SUBSTR(ts, 1, 10) as day,
                  SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) as tokens,
                  SUM(COALESCE(cost_usd, 0)) as cost
           FROM readings
           WHERE ts >= ?
           GROUP BY day
           ORDER BY day ASC""",
        (_cutoff_iso("7d"),),
    ).fetchall()

    daily = [{"day": r["day"], "tokens": r["tokens"], "cost": r["cost"]} for r in rows]

    if daily:
        avg_cost = sum(d["cost"] for d in daily) / len(daily)
    else:
        avg_cost = 0.0

    projected_30d = avg_cost * 30

    return {
        "daily": daily,
        "avg_daily_cost": avg_cost,
        "projected_30d": projected_30d,
    }


# ---------------------------------------------------------------------------
# Readings list (paginated)
# ---------------------------------------------------------------------------

def get_readings_page(
    conn: sqlite3.Connection,
    page: int = 1,
    per_page: int = 50,
    status_filter: Optional[str] = None,
    range_str: Optional[str] = None,
) -> dict:
    """Return paginated readings with per-lens rows."""
    conditions = []
    params: list = []

    if range_str:
        conditions.append("r.ts >= ?")
        params.append(_cutoff_iso(range_str))
    if status_filter:
        conditions.append("r.status = ?")
        params.append(status_filter)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = conn.execute(
        f"SELECT COUNT(*) as n FROM readings r {where}", params
    ).fetchone()["n"]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT r.* FROM readings r {where} ORDER BY r.ts DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    reading_ids = [r["id"] for r in rows]
    lens_by_reading: dict[int, list] = {}
    if reading_ids:
        placeholders = ",".join("?" * len(reading_ids))
        lr = conn.execute(
            f"SELECT * FROM lens_readings WHERE reading_id IN ({placeholders})",
            reading_ids,
        ).fetchall()
        for lrow in lr:
            lens_by_reading.setdefault(lrow["reading_id"], []).append(dict(lrow))

    readings_out = []
    for r in rows:
        rd = dict(r)
        rd["lenses"] = lens_by_reading.get(rd["id"], [])
        readings_out.append(rd)

    return {
        "readings": readings_out,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


# ---------------------------------------------------------------------------
# Reading detail
# ---------------------------------------------------------------------------

def get_reading(conn: sqlite3.Connection, reading_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    lenses = conn.execute(
        "SELECT * FROM lens_readings WHERE reading_id = ?", (reading_id,)
    ).fetchall()
    d["lenses"] = [dict(lr) for lr in lenses]
    return d


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def get_alerts(
    conn: sqlite3.Connection,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    total = conn.execute("SELECT COUNT(*) as n FROM alerts").fetchone()["n"]
    offset = (page - 1) * per_page
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY ts DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    ).fetchall()
    return {
        "alerts": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }
