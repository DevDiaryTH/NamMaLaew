"""Tests for wlm.rain_around: destination_point, fetch/parse, cache, and API endpoint."""

from __future__ import annotations

import importlib
import json
import sys
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest_web import (
    tmp_db, tmp_snap_dir, demo_db, app_env, no_password_env,
    make_client, get_session_cookie,
)
import wlm.rain_around as rain_around_mod
from wlm.rain_around import destination_point, fetch_rain_around, get_rain_around, ALLOWED_RADII


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_urlopen(payload):
    """Return a context manager that yields a response-like object (matches test_core_weather.py)."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_location(precip_now=0.0, wind_deg=0.0, wind_kmh=0.0,
                   current_time="2026-09-26T16:00",
                   hourly_times=None, hourly_precip=None):
    """Build a single Open-Meteo location dict."""
    if hourly_times is None:
        hourly_times = [
            "2026-09-26T13:00", "2026-09-26T14:00", "2026-09-26T15:00",
            "2026-09-26T16:00", "2026-09-26T17:00", "2026-09-26T18:00",
        ]
    if hourly_precip is None:
        hourly_precip = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return {
        "current": {
            "time": current_time,
            "interval": 900,
            "precipitation": precip_now,
            "wind_direction_10m": wind_deg,
            "wind_speed_10m": wind_kmh,
        },
        "hourly": {
            "time": hourly_times,
            "precipitation": hourly_precip,
        },
    }


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level cache before each test."""
    rain_around_mod._cache.clear()
    yield
    rain_around_mod._cache.clear()


# ---------------------------------------------------------------------------
# destination_point
# ---------------------------------------------------------------------------

class TestDestinationPoint:
    def test_north_increases_lat(self):
        lat, lon = destination_point(13.75, 100.5, 0, 25)
        # 25 km north ≈ +0.2248° latitude
        assert abs(lat - (13.75 + 0.2248)) < 1e-3
        # Longitude unchanged when heading due north
        assert abs(lon - 100.5) < 1e-3

    def test_east_increases_lon(self):
        lat, lon = destination_point(13.75, 100.5, 90, 25)
        # Longitude increases eastward
        assert lon > 100.5
        # Latitude barely changes when heading due east
        assert abs(lat - 13.75) < 1e-3

    def test_south_decreases_lat(self):
        lat, lon = destination_point(13.75, 100.5, 180, 25)
        assert lat < 13.75
        assert abs(lon - 100.5) < 1e-3

    def test_west_decreases_lon(self):
        lat, lon = destination_point(13.75, 100.5, 270, 25)
        assert lon < 100.5
        assert abs(lat - 13.75) < 1e-3


# ---------------------------------------------------------------------------
# fetch_rain_around — parse logic
# ---------------------------------------------------------------------------

class TestFetchRainAround:
    def _build_payload(self, center_precip=1.1, ne_precip_now=3.0,
                       ne_past=[0.3, 1.1, 3.9], ne_next=[5.2, 4.2, 1.5]):
        """Build a 9-location payload mimicking the real Open-Meteo response."""
        hourly_times = [
            "2026-09-26T13:00", "2026-09-26T14:00", "2026-09-26T15:00",
            "2026-09-26T16:00", "2026-09-26T17:00", "2026-09-26T18:00",
        ]
        # center_time is "2026-09-26T16:30" → floor to 16:00
        current_time = "2026-09-26T16:30"

        center = _make_location(
            precip_now=center_precip, wind_deg=182, wind_kmh=18.4,
            current_time=current_time,
            hourly_times=hourly_times,
            hourly_precip=ne_past + ne_next,
        )
        ne = _make_location(
            precip_now=ne_precip_now,
            current_time=current_time,
            hourly_times=hourly_times,
            hourly_precip=ne_past + ne_next,
        )
        # Other 7 dirs: rainy at S only (precip_now=0.2)
        plain = [
            _make_location(precip_now=0.0, current_time=current_time,
                           hourly_times=hourly_times,
                           hourly_precip=[0.0]*6)
            for _ in range(6)
        ]
        s_dir = _make_location(precip_now=0.2, current_time=current_time,
                               hourly_times=hourly_times,
                               hourly_precip=[0.0]*6)
        # Order: center, N, NE, E, SE, S, SW, W, NW
        # plain[0..5] fill N, E, SE, SW, W, NW; ne fills NE; s_dir fills S
        return [center] + [plain[0]] + [ne] + plain[1:3] + [s_dir] + plain[3:6]

    def test_parses_directions(self):
        payload = self._build_payload()
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)

        assert result["radius_km"] == 25
        assert len(result["directions"]) == 8
        assert result["directions"][0]["dir"] == "N"
        assert result["directions"][1]["dir"] == "NE"

    def test_center_fields(self):
        payload = self._build_payload(center_precip=1.1)
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)

        c = result["center"]
        assert c["precip_now"] == pytest.approx(1.1)
        assert c["wind_from_deg"] == pytest.approx(182)
        assert c["wind_kmh"] == pytest.approx(18.4)

    def test_past_next_split(self):
        # current_time "16:30" → boundary at 16:00
        # hours 13,14,15 < 16:00 → past3h; hours 16,17,18 >= 16:00 → next3h
        past = [0.3, 1.1, 3.9]   # sum = 5.3
        nxt  = [5.2, 4.2, 1.5]   # sum = 10.9
        payload = self._build_payload(ne_past=past, ne_next=nxt)
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)

        ne = next(d for d in result["directions"] if d["dir"] == "NE")
        assert ne["precip_past3h"] == pytest.approx(sum(past), rel=1e-4)
        assert ne["precip_next3h"] == pytest.approx(sum(nxt),  rel=1e-4)

    def test_rain_from_ordering(self):
        # NE: now=3.0, past sum=5.3 → total 8.3
        # S:  now=0.2, past=0    → total 0.2
        # NE should appear first in rain_from
        payload = self._build_payload(ne_precip_now=3.0)
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)

        assert result["rain_from"][0] == "NE"
        assert "S" in result["rain_from"]

    def test_none_precip_counts_as_zero(self):
        hourly_times = [
            "2026-09-26T13:00", "2026-09-26T14:00", "2026-09-26T15:00",
            "2026-09-26T16:00", "2026-09-26T17:00", "2026-09-26T18:00",
        ]
        loc = _make_location(
            precip_now=None, current_time="2026-09-26T16:30",
            hourly_times=hourly_times, hourly_precip=[None]*6,
        )
        payload = [loc] * 9
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)

        assert result["center"]["precip_now"] == pytest.approx(0.0)
        assert result["rain_from"] == []

    def test_fetched_at_is_iso_utc(self):
        payload = self._build_payload()
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_around(13.75, 100.5, 25)
        # Should parse as a valid ISO datetime string
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(result["fetched_at"])
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# get_rain_around — settings + cache
# ---------------------------------------------------------------------------

class TestGetRainAround:
    def test_returns_none_with_blank_lat(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE",  "")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db
        assert get_rain_around(25, conn=conn) is None

    def test_returns_none_with_blank_lon(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE",  "13.75")
        monkeypatch.setenv("LONGITUDE", "")
        conn, _ = tmp_db
        assert get_rain_around(25, conn=conn) is None

    def test_returns_none_with_unparseable_coords(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE",  "not_a_float")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db
        assert get_rain_around(25, conn=conn) is None

    def _rain_payload(self):
        hourly_times = [
            "2026-09-26T13:00", "2026-09-26T14:00", "2026-09-26T15:00",
            "2026-09-26T16:00", "2026-09-26T17:00", "2026-09-26T18:00",
        ]
        loc = _make_location(hourly_times=hourly_times, hourly_precip=[0.0]*6)
        return [loc] * 9

    def test_cache_second_call_no_http(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE",  "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db

        payload = self._rain_payload()
        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)) as mock_open:
            get_rain_around(25, conn=conn)
            get_rain_around(25, conn=conn)
            assert mock_open.call_count == 1

    def test_cache_expires_after_ttl(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE",  "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db

        payload = self._rain_payload()
        # First call stores ts=0.0.
        # Second call checks: monotonic() returns TTL+1, so TTL+1 - 0.0 > TTL → re-fetch.
        # Third call (second store) gets TTL+1 as well.
        times = [0.0, rain_around_mod._CACHE_TTL_SECONDS + 1, rain_around_mod._CACHE_TTL_SECONDS + 1]
        call_idx = [0]

        def fake_monotonic():
            val = times[min(call_idx[0], len(times) - 1)]
            call_idx[0] += 1
            return val

        with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)) as mock_open:
            with patch("wlm.rain_around.time.monotonic", side_effect=fake_monotonic):
                get_rain_around(25, conn=conn)   # first fetch; stores ts via monotonic() → 0.0
                get_rain_around(25, conn=conn)   # second: check monotonic()→TTL+1, expired → re-fetch
                assert mock_open.call_count == 2


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

def reload_app():
    for mod in list(sys.modules.keys()):
        if mod.startswith("web.app") or mod == "web.app":
            del sys.modules[mod]
    import web.app
    return web.app.app


@pytest.mark.asyncio
async def test_rain_around_bad_radius(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/rain-around?radius=7")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rain_around_unauthenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/rain-around?radius=25")
        # Unauthenticated → redirect (302) or 401/403
        assert resp.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_rain_around_enabled_false_blank_coords(app_env, monkeypatch):
    """When lat/lon are blank, endpoint returns {"enabled": false}."""
    monkeypatch.setenv("LATITUDE",  "")
    monkeypatch.setenv("LONGITUDE", "")
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/rain-around?radius=25")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}


@pytest.mark.asyncio
async def test_rain_around_502_on_fetch_error(app_env, monkeypatch):
    monkeypatch.setenv("LATITUDE",  "13.75")
    monkeypatch.setenv("LONGITUDE", "100.5")
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    with patch("wlm.rain_around.urllib.request.urlopen", side_effect=Exception("network down")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookies = await get_session_cookie(c)
            c.cookies.update(cookies)
            resp = await c.get("/api/rain-around?radius=25")
            assert resp.status_code == 502
            assert "unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rain_around_200_shape(app_env, monkeypatch):
    monkeypatch.setenv("LATITUDE",  "13.75")
    monkeypatch.setenv("LONGITUDE", "100.5")

    hourly_times = [
        "2026-09-26T13:00", "2026-09-26T14:00", "2026-09-26T15:00",
        "2026-09-26T16:00", "2026-09-26T17:00", "2026-09-26T18:00",
    ]
    loc = _make_location(hourly_times=hourly_times, hourly_precip=[0.1]*6)
    payload = [loc] * 9

    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    with patch("wlm.rain_around.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookies = await get_session_cookie(c)
            c.cookies.update(cookies)
            resp = await c.get("/api/rain-around?radius=25")

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["radius_km"] == 25
    assert "center" in data
    assert "directions" in data
    assert len(data["directions"]) == 8
    assert "rain_from" in data
    assert "fetched_at" in data
