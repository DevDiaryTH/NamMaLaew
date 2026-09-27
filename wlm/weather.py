"""Rainfall data from Open-Meteo, at most every 30 minutes."""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from wlm import db, settings

logger = logging.getLogger("wlm.weather")

_INTERVAL_SECONDS = 30 * 60
_STATE_KEY = "last_weather_fetch_ts"


def refresh_weather(conn=None) -> None:
    """Fetch precipitation forecast if latitude/longitude are set and 30 min has elapsed."""
    lat = settings.get("latitude", conn=conn).strip()
    lon = settings.get("longitude", conn=conn).strip()
    if not lat or not lon:
        return

    now = datetime.now(timezone.utc)
    last_ts_str = db.get_state(_STATE_KEY, default="", conn=conn) or ""
    if last_ts_str:
        try:
            last_ts = datetime.fromisoformat(last_ts_str)
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if (now - last_ts).total_seconds() < _INTERVAL_SECONDS:
                logger.debug("Weather fetch throttled (< 30 min since last fetch)")
                return
        except (ValueError, TypeError):
            pass

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=precipitation&past_days=2&forecast_days=1&timezone=UTC"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("Weather fetch failed: %s", exc)
        return

    try:
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        precip = hourly.get("precipitation", [])
        for hour_ts_str, mm in zip(times, precip):
            # Normalize to UTC ISO with offset
            try:
                # Open-Meteo returns "2026-09-25T14:00" (no offset) when timezone=UTC
                dt = datetime.fromisoformat(hour_ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                hour_ts = dt.isoformat(timespec="seconds")
                db.upsert_weather(hour_ts, float(mm) if mm is not None else None, conn=conn)
            except Exception as e:
                logger.debug("Skipping weather row %s: %s", hour_ts_str, e)

        db.set_state(_STATE_KEY, now.isoformat(timespec="seconds"), conn=conn)
        logger.info("Weather data refreshed (%d hours)", len(times))
    except Exception as exc:
        logger.warning("Weather parse failed: %s", exc)
