"""Rainfall at 8 compass points around the site — one Open-Meteo multi-location call."""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from wlm import settings

logger = logging.getLogger("wlm.rain_around")

# Allowed radius values exposed to callers.
ALLOWED_RADII = (10, 25, 50)

_EARTH_R_KM = 6371.0
_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
_BEARINGS = [
    ("N",   0),
    ("NE", 45),
    ("E",  90),
    ("SE", 135),
    ("S",  180),
    ("SW", 225),
    ("W",  270),
    ("NW", 315),
]

# Module-level cache: (lat, lon, radius_km) -> {"ts": monotonic, "data": dict}
_cache: dict[tuple, dict] = {}


def destination_point(lat: float, lon: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Return the lat/lon reached by travelling dist_km along bearing_deg from (lat, lon).

    Uses the spherical-Earth (great-circle) formula with Earth radius 6371.0 km.
    """
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    b_r   = math.radians(bearing_deg)
    d_r   = dist_km / _EARTH_R_KM

    dest_lat_r = math.asin(
        math.sin(lat_r) * math.cos(d_r)
        + math.cos(lat_r) * math.sin(d_r) * math.cos(b_r)
    )
    dest_lon_r = lon_r + math.atan2(
        math.sin(b_r) * math.sin(d_r) * math.cos(lat_r),
        math.cos(d_r) - math.sin(lat_r) * math.sin(dest_lat_r),
    )
    return (math.degrees(dest_lat_r), math.degrees(dest_lon_r))


def fetch_rain_around(lat: float, lon: float, radius_km: int) -> dict:
    """Fetch current + 3-hour rainfall for site center and 8 compass points.

    Sends one request with 9 comma-separated coordinates.  Open-Meteo returns
    a JSON list when multiple locations are requested (always true here).

    Returns a dict with keys: radius_km, center, directions, rain_from, fetched_at.
    Raises on any HTTP or parse failure — caller maps to 502.
    """
    # Build point list: index 0 = center, then N..NW
    points = [(lat, lon)]
    for _dir, bearing in _BEARINGS:
        points.append(destination_point(lat, lon, bearing, radius_km))

    lats = ",".join(f"{p[0]:.5f}" for p in points)
    lons = ",".join(f"{p[1]:.5f}" for p in points)

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lons}"
        "&hourly=precipitation"
        "&past_hours=3&forecast_hours=3"
        "&timezone=UTC"
        "&current=precipitation,wind_direction_10m,wind_speed_10m"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read())

    # Response should be a list of 9 objects; handle an accidental dict defensively.
    if isinstance(raw, dict):
        raw = [raw]

    if len(raw) < 9:
        raise ValueError(f"Expected 9 location objects, got {len(raw)}")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _current_val(obj: dict, key: str, default=0.0):
        return obj.get("current", {}).get(key) or default

    def _split_hourly(obj: dict, current_time_str: str) -> tuple[float, float]:
        """Return (past3h_sum, next3h_sum) based on current_time floored to the hour."""
        try:
            # Floor current time to the hour for the split boundary.
            ct = datetime.fromisoformat(current_time_str)
            boundary = ct.replace(minute=0, second=0, microsecond=0)
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
        except Exception:
            return 0.0, 0.0

        hourly = obj.get("hourly", {})
        times  = hourly.get("time", [])
        precip = hourly.get("precipitation", [])

        past3h = 0.0
        next3h = 0.0
        for ts_str, mm in zip(times, precip):
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                val = float(mm) if mm is not None else 0.0
                if dt < boundary:
                    past3h += val
                else:
                    next3h += val
            except Exception:
                continue
        return past3h, next3h

    # --- Parse center (index 0) ---
    center_obj = raw[0]
    center_time = center_obj.get("current", {}).get("time", "")
    center_precip    = round(_current_val(center_obj, "precipitation"), 2)
    center_wind_deg  = round(_current_val(center_obj, "wind_direction_10m"), 2)
    center_wind_kmh  = round(_current_val(center_obj, "wind_speed_10m"), 2)

    center = {
        "lat":          round(lat, 5),
        "lon":          round(lon, 5),
        "precip_now":   center_precip,
        "wind_from_deg": center_wind_deg,
        "wind_kmh":     center_wind_kmh,
    }

    # --- Parse 8 directional points (indices 1-8) ---
    directions = []
    for i, ((dir_name, bearing), point) in enumerate(zip(_BEARINGS, points[1:])):
        obj = raw[i + 1]
        precip_now = round(_current_val(obj, "precipitation"), 2)
        past3h, next3h = _split_hourly(obj, center_time)
        directions.append({
            "dir":           dir_name,
            "bearing":       bearing,
            "lat":           round(point[0], 5),
            "lon":           round(point[1], 5),
            "precip_now":    precip_now,
            "precip_past3h": round(past3h, 2),
            "precip_next3h": round(next3h, 2),
        })

    # Directions where rain is arriving: precip_now >= 0.1 or precip_past3h >= 0.1,
    # sorted by (precip_now + precip_past3h) descending.
    rain_from = sorted(
        [d["dir"] for d in directions if d["precip_now"] >= 0.1 or d["precip_past3h"] >= 0.1],
        key=lambda name: next(
            -(d["precip_now"] + d["precip_past3h"])
            for d in directions if d["dir"] == name
        ),
    )

    return {
        "radius_km":  radius_km,
        "center":     center,
        "directions": directions,
        "rain_from":  rain_from,
        "fetched_at": fetched_at,
    }


def get_rain_around(radius_km: int, conn=None) -> Optional[dict]:
    """Return cached or fresh rain-around data, or None if coords are not configured.

    Results are cached per (lat, lon, radius_km) for 15 minutes using
    time.monotonic() so the wall clock cannot be spoofed by DST changes.
    """
    lat_str = settings.get("latitude",  conn=conn).strip()
    lon_str = settings.get("longitude", conn=conn).strip()
    if not lat_str or not lon_str:
        return None

    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        logger.debug("rain_around: unparseable coords lat=%r lon=%r", lat_str, lon_str)
        return None

    cache_key = (lat, lon, radius_km)
    entry = _cache.get(cache_key)
    if entry and (time.monotonic() - entry["ts"]) < _CACHE_TTL_SECONDS:
        logger.debug("rain_around: cache hit for %s", cache_key)
        return entry["data"]

    logger.debug("rain_around: fetching for lat=%.5f lon=%.5f radius=%d km", lat, lon, radius_km)
    data = fetch_rain_around(lat, lon, radius_km)
    _cache[cache_key] = {"ts": time.monotonic(), "data": data}
    return data
