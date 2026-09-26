"""5×5 forecast-grid rainfall — one Open-Meteo multi-location call."""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from wlm import settings

logger = logging.getLogger("wlm.rain_forecast")

# Allowed radius values exposed to callers.
ALLOWED_RADII = (10, 25, 50)
GRID_SIZE = 5

_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes

# Module-level cache: (lat, lon, radius_km) -> {"ts": monotonic, "data": dict}
_cache: dict[tuple, dict] = {}


def grid_points(
    lat: float, lon: float, radius_km: float, n: int = GRID_SIZE
) -> tuple[float, list[tuple[float, float]]]:
    """Return (step_km, [(lat, lon), ...]) for an n×n square grid.

    Points are ordered row-major north→south, west→east. The centre cell
    (n // 2, n // 2) equals the site. step_km = 2 * radius_km / (n − 1).

    Uses a local flat-earth approximation (111.32 km per degree of latitude,
    scaled by cos(lat) for longitude) — accurate to within ~1 % at ≤ 50 km.
    """
    step_km = 2 * radius_km / (n - 1)
    half = (n - 1) / 2  # 2.0 for n=5

    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(lat))

    points: list[tuple[float, float]] = []
    for i in range(n):  # row: 0 = northernmost … n-1 = southernmost
        lat_offset_deg = (half - i) * step_km / km_per_deg_lat
        for j in range(n):  # col: 0 = westernmost … n-1 = easternmost
            lon_offset_deg = (j - half) * step_km / km_per_deg_lon
            points.append((lat + lat_offset_deg, lon + lon_offset_deg))

    return step_km, points


def fetch_rain_forecast(lat: float, lon: float, radius_km: int) -> dict:
    """Fetch a 3-hour hourly precipitation forecast for a 5×5 grid around the site.

    Sends one request with 25 comma-separated coordinates. Open-Meteo returns
    a JSON list when multiple locations are requested (always true here).

    Open-Meteo hourly precipitation at time T is the total of the preceding hour
    (T−1 h … T). Only entries whose time is strictly after current.time are kept,
    giving the three future forecast windows. Fewer than 3 windows is fine —
    the function returns what is available rather than raising.

    Returns a dict with keys: radius_km, step_km, hours, cells, fetched_at.
    Raises on any HTTP or parse failure, or wrong location count.
    """
    step_km, points = grid_points(lat, lon, radius_km)
    n_points = GRID_SIZE * GRID_SIZE

    lats = ",".join(f"{p[0]:.5f}" for p in points)
    lons = ",".join(f"{p[1]:.5f}" for p in points)

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lons}"
        "&hourly=precipitation"
        "&forecast_hours=4"
        "&timezone=UTC"
        "&current=precipitation"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read())

    # Response should be a list of 25 objects; handle an accidental dict defensively.
    if isinstance(raw, dict):
        raw = [raw]

    if len(raw) != n_points:
        raise ValueError(f"Expected {n_points} location objects, got {len(raw)}")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Determine the shared future-hours list from the first location object.
    current_time_str = raw[0].get("current", {}).get("time", "")
    try:
        current_dt = datetime.fromisoformat(current_time_str)
        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)
    except Exception as exc:
        raise ValueError(
            f"Cannot parse current time from first location: {current_time_str!r}"
        ) from exc

    first_hourly_times = raw[0].get("hourly", {}).get("time", [])

    # Collect hours strictly after current.time; cap at 3 future windows.
    future_dts: list[datetime] = []
    for ts_str in first_hourly_times:
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > current_dt:
                future_dts.append(dt)
        except Exception:
            continue

    future_dts = future_dts[:3]

    # Format as ISO UTC with explicit offset, e.g. "2026-09-26T18:00:00+00:00".
    hours = [dt.strftime("%Y-%m-%dT%H:%M:%S+00:00") for dt in future_dts]

    # Build one cell per point, aligning precipitation values to the shared hour list.
    cells = []
    for obj, point in zip(raw, points):
        hourly_times = obj.get("hourly", {}).get("time", [])
        hourly_precip = obj.get("hourly", {}).get("precipitation", [])

        # Lookup by naive-UTC string "YYYY-MM-DDTHH:MM" (Open-Meteo format).
        time_to_mm: dict[str, object] = dict(zip(hourly_times, hourly_precip))

        mm_values = []
        for ft in future_dts:
            ts_key = ft.strftime("%Y-%m-%dT%H:%M")
            mm = time_to_mm.get(ts_key)
            mm_values.append(round(float(mm) if mm is not None else 0.0, 2))

        cells.append({
            "lat": round(point[0], 5),
            "lon": round(point[1], 5),
            "mm": mm_values,
        })

    return {
        "radius_km": radius_km,
        "step_km": round(step_km, 2),
        "hours": hours,
        "cells": cells,
        "fetched_at": fetched_at,
    }


def get_rain_forecast(radius_km: int, conn=None) -> Optional[dict]:
    """Return cached or fresh rain-forecast data, or None if coords are not configured.

    Results are cached per (lat, lon, radius_km) for 30 minutes.
    """
    lat_str = settings.get("latitude", conn=conn).strip()
    lon_str = settings.get("longitude", conn=conn).strip()
    if not lat_str or not lon_str:
        return None

    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        logger.debug("rain_forecast: unparseable coords lat=%r lon=%r", lat_str, lon_str)
        return None

    cache_key = (lat, lon, radius_km)
    entry = _cache.get(cache_key)
    if entry and (time.monotonic() - entry["ts"]) < _CACHE_TTL_SECONDS:
        logger.debug("rain_forecast: cache hit for %s", cache_key)
        return entry["data"]

    logger.debug(
        "rain_forecast: fetching for lat=%.5f lon=%.5f radius=%d km", lat, lon, radius_km
    )
    data = fetch_rain_forecast(lat, lon, radius_km)
    _cache[cache_key] = {"ts": time.monotonic(), "data": data}
    return data
