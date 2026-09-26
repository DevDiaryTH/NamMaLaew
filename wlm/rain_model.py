"""Learn the site-specific rain→water-level response from historical data.

Sample definition
-----------------
For each UTC hour H where:
  - weather rows for hours H-3, H-2, H-1 all exist (3 full hours of rain data)
  - a reading exists within ±15 min of H (the "before" level)
  - a reading exists within ±15 min of H + horizon_hours (the "after" level)

The sample is:
  rain_3h_mm  = precipitation_mm for H-3 + H-2 + H-1
  delta_level = level(H+horizon) − level(H)

Levels come from readings.level_index (non-NULL only). When feedback.verdict
is 'wrong' for a reading, the corrected true_level_index replaces the raw value.

Model
-----
Least-squares fit: delta_level = a * rain_3h_mm + b
Requires at least min_rain_samples samples where rain_3h_mm > 0.1 mm.
Returns {a, b, n, n_rain, r2, fitted_at} or None.
"""

from __future__ import annotations

import bisect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from wlm import db

logger = logging.getLogger("wlm.rain_model")

_MODEL_STATE_KEY = "rain_model"
_NEAR_MINUTES = 15  # ±window for matching readings to hour boundaries


def _now_utc(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hour_start(dt: datetime) -> datetime:
    """Return the UTC hour boundary (floor to hour)."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _corrected_levels(conn) -> dict[int, float]:
    """Return {reading_id: true_level_index} for feedback rows with verdict='wrong'."""
    rows = conn.execute(
        "SELECT reading_id, true_level_index FROM feedback "
        "WHERE verdict = 'wrong' AND true_level_index IS NOT NULL"
    ).fetchall()
    return {r["reading_id"]: r["true_level_index"] for r in rows}


def _nearest_level(times: list[float], levels: list[float], ts_target: datetime,
                   window_minutes: int = _NEAR_MINUTES) -> float | None:
    """Level of the reading nearest to ts_target within ±window_minutes, or None.

    times must be sorted ascending (epoch seconds) and aligned with levels.
    """
    target = ts_target.timestamp()
    i = bisect.bisect_left(times, target)
    best = None
    best_delta = None
    for j in (i - 1, i):
        if 0 <= j < len(times):
            delta = abs(times[j] - target)
            if delta <= window_minutes * 60 and (best_delta is None or delta < best_delta):
                best, best_delta = levels[j], delta
    return best


def build_samples(conn, horizon_hours: int = 2) -> list[tuple[float, float]]:
    """Return list of (rain_3h_mm, delta_level) samples from historical data.

    For each UTC hour H with weather rows at H-3, H-2, H-1 and readings within
    ±15 min of both H and H+horizon_hours, emit one sample.  The level values
    are corrected by human feedback where available.  Samples where either
    level is NULL are skipped.
    """
    corrections = _corrected_levels(conn)

    # Load all readings with non-NULL level_index
    reading_rows = conn.execute(
        "SELECT id, ts, level_index FROM readings "
        "WHERE level_index IS NOT NULL ORDER BY ts ASC"
    ).fetchall()

    # Load all weather rows into a dict keyed by hour_ts string
    weather_rows = conn.execute(
        "SELECT hour_ts, precipitation_mm FROM weather "
        "WHERE precipitation_mm IS NOT NULL"
    ).fetchall()
    weather_by_hour: dict[str, float] = {}
    for w in weather_rows:
        try:
            dt = _parse_ts(w["hour_ts"])
            key = _hour_start(dt).isoformat(timespec="seconds")
            weather_by_hour[key] = float(w["precipitation_mm"])
        except (ValueError, TypeError):
            continue

    times: list[float] = []
    levels: list[float] = []
    for r in reading_rows:
        try:
            dt = _parse_ts(r["ts"])
        except (ValueError, TypeError):
            continue
        level = corrections.get(r["id"], r["level_index"])
        if level is not None:
            times.append(dt.timestamp())
            levels.append(level)
    order = sorted(range(len(times)), key=times.__getitem__)
    times = [times[k] for k in order]
    levels = [levels[k] for k in order]

    samples = []
    for hour_ts_str in list(weather_by_hour.keys()):
        try:
            H = _parse_ts(hour_ts_str)
        except (ValueError, TypeError):
            continue

        # We need rain for H-3, H-2, H-1
        rain_hours = []
        for offset in (3, 2, 1):
            prev_h = _hour_start(H - timedelta(hours=offset))
            key = prev_h.isoformat(timespec="seconds")
            if key not in weather_by_hour:
                break
            rain_hours.append(weather_by_hour[key])

        if len(rain_hours) < 3:
            continue

        rain_3h = sum(rain_hours)

        # Find readings near H and H+horizon
        level_before = _nearest_level(times, levels, H)
        level_after = _nearest_level(times, levels, H + timedelta(hours=horizon_hours))

        if level_before is None or level_after is None:
            continue

        samples.append((rain_3h, level_after - level_before))

    return samples


def _least_squares(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Return (a, b, r2) for y = a*x + b."""
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n, 0.0
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n

    y_mean = sy / n
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    if ss_tot == 0:
        r2 = 1.0
    else:
        ss_res = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - ss_res / ss_tot

    return a, b, r2


def fit_rain_response(
    conn,
    min_rain_samples: int = 20,
    now: datetime | None = None,
) -> dict | None:
    """Fit delta_level = a*rain + b over all samples.

    Requires at least min_rain_samples samples with rain_3h_mm > 0.1 mm.
    Returns {a, b, n, n_rain, r2, fitted_at} or None when insufficient data.
    """
    samples = build_samples(conn)
    rain_samples = [(r, d) for r, d in samples if r > 0.1]
    n_rain = len(rain_samples)

    if n_rain < min_rain_samples:
        return None

    xs = [r for r, _ in rain_samples]
    ys = [d for _, d in rain_samples]
    a, b, r2 = _least_squares(xs, ys)

    fitted_at = _now_utc(now).isoformat(timespec="seconds")
    return {
        "a": a,
        "b": b,
        "n": len(samples),
        "n_rain": n_rain,
        "r2": r2,
        "fitted_at": fitted_at,
    }


def maybe_refit(conn, now: datetime | None = None, max_age_hours: int = 24) -> dict:
    """Refit at most once per day; store result in runtime_state key 'rain_model'.

    When there is insufficient data, stores a status dict instead of the model.
    Returns the stored dict.
    """
    now_dt = _now_utc(now)
    stored_json = db.get_state(_MODEL_STATE_KEY, conn=conn)
    if stored_json:
        try:
            stored = json.loads(stored_json)
            fitted_at_str = stored.get("fitted_at")
            if fitted_at_str:
                fitted_at = _parse_ts(fitted_at_str)
                age = (now_dt - fitted_at).total_seconds() / 3600.0
                if age < max_age_hours:
                    return stored
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    model = fit_rain_response(conn, now=now_dt)
    if model is None:
        n_rain = sum(1 for r, _ in build_samples(conn) if r > 0.1)
        result: dict = {
            "status": "insufficient_data",
            "n_rain": n_rain,
            "needed": 20,
            "fitted_at": now_dt.isoformat(timespec="seconds"),
        }
    else:
        result = model

    db.set_state(_MODEL_STATE_KEY, json.dumps(result), conn=conn)
    return result


def load_model(conn) -> dict | None:
    """Load the stored model dict, or None if absent or insufficient_data."""
    stored_json = db.get_state(_MODEL_STATE_KEY, conn=conn)
    if not stored_json:
        return None
    try:
        d = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    if d.get("status") == "insufficient_data":
        return None
    if "a" not in d or "b" not in d:
        return None
    return d


def predict_rise(
    conn,
    now: datetime | None = None,
    horizon_hours: int = 3,
) -> dict | None:
    """Predict the water-level rise over the next horizon_hours from forecast rain.

    Needs a fitted model, a current level, and forecast weather rows.

    forecast_rain_mm = sum of precipitation_mm for weather hours in [now, now+horizon).
    predicted_rise   = max(0, a*rain + b)  — clamped to zero because negative
                       rain-driven rise is not physically meaningful as a forecast.
    predicted_level  = min(100, current_level + predicted_rise)  — clamped to scale.

    Returns {forecast_rain_mm, predicted_rise, current_level, predicted_level, n, r2}
    or None when model/level/forecast is unavailable.
    """
    model = load_model(conn)
    if model is None:
        return None

    now_dt = _now_utc(now)
    horizon_end = now_dt + timedelta(hours=horizon_hours)

    # Current level: latest non-null reading
    row = conn.execute(
        "SELECT level_index FROM readings WHERE level_index IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    current_level = row["level_index"]

    # Sum forecast rain in [now, now+horizon)
    # We include any weather hour whose hour_start falls in that window.
    weather_rows = conn.execute(
        "SELECT hour_ts, precipitation_mm FROM weather "
        "WHERE precipitation_mm IS NOT NULL"
    ).fetchall()

    forecast_rain = 0.0
    n_forecast = 0
    for w in weather_rows:
        try:
            h_dt = _parse_ts(w["hour_ts"])
        except (ValueError, TypeError):
            continue
        h_start = _hour_start(h_dt)
        if now_dt <= h_start < horizon_end:
            forecast_rain += float(w["precipitation_mm"])
            n_forecast += 1

    if n_forecast == 0:
        return None

    a = model["a"]
    b = model["b"]
    # Clamp: negative predicted_rise is not meaningful for a forecast
    predicted_rise = max(0.0, a * forecast_rain + b)
    predicted_level = min(100.0, current_level + predicted_rise)

    return {
        "forecast_rain_mm": forecast_rain,
        "predicted_rise": predicted_rise,
        "current_level": current_level,
        "predicted_level": predicted_level,
        "n": model["n"],
        "n_rain": model["n_rain"],
        "r2": model["r2"],
    }
