"""Tests for wlm.rain_forecast: grid_points, fetch/parse, cache, and API endpoint."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest_web import (
    tmp_db, tmp_snap_dir, demo_db, app_env, no_password_env,
    make_client, get_session_cookie,
)
import wlm.rain_forecast as rain_forecast_mod
from wlm.rain_forecast import (
    grid_points, fetch_rain_forecast, get_rain_forecast,
    ALLOWED_RADII, GRID_SIZE,
)


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


def _make_location(current_time="2026-09-26T17:15",
                   hourly_times=None, hourly_precip=None):
    """Build a single Open-Meteo location dict for forecast_hours=4."""
    if hourly_times is None:
        hourly_times = [
            "2026-09-26T17:00",
            "2026-09-26T18:00",
            "2026-09-26T19:00",
            "2026-09-26T20:00",
        ]
    if hourly_precip is None:
        hourly_precip = [0.0, 0.0, 0.0, 0.0]
    return {
        "current": {
            "time": current_time,
            "interval": 900,
            "precipitation": 0.0,
        },
        "hourly": {
            "time": hourly_times,
            "precipitation": hourly_precip,
        },
    }


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level cache before each test."""
    rain_forecast_mod._cache.clear()
    yield
    rain_forecast_mod._cache.clear()


# ---------------------------------------------------------------------------
# grid_points
# ---------------------------------------------------------------------------

class TestGridPoints:
    def test_returns_25_points(self):
        step_km, points = grid_points(13.75, 100.5, 25)
        assert len(points) == GRID_SIZE * GRID_SIZE

    def test_centre_equals_site(self):
        lat, lon = 13.75, 100.5
        _step_km, points = grid_points(lat, lon, 25)
        # Centre is row 2, col 2 → index 2*5+2 = 12
        centre = points[GRID_SIZE // 2 * GRID_SIZE + GRID_SIZE // 2]
        assert abs(centre[0] - lat) < 1e-9
        assert abs(centre[1] - lon) < 1e-9

    def test_north_row_higher_lat(self):
        _step_km, points = grid_points(13.75, 100.5, 25)
        north_lat = points[0][0]                          # row 0, col 0
        south_lat = points[(GRID_SIZE - 1) * GRID_SIZE][0]  # row 4, col 0
        assert north_lat > south_lat

    def test_west_col_lower_lon(self):
        _step_km, points = grid_points(13.75, 100.5, 25)
        west_lon = points[0][1]              # row 0, col 0 (westernmost)
        east_lon = points[GRID_SIZE - 1][1]  # row 0, col 4 (easternmost)
        assert west_lon < east_lon

    def test_step_km(self):
        radius = 25
        step_km, _points = grid_points(13.75, 100.5, radius)
        expected = 2 * radius / (GRID_SIZE - 1)
        assert abs(step_km - expected) < 1e-9

    def test_ns_span_approx_2r(self):
        lat, lon = 13.75, 100.5
        radius = 25
        _step_km, points = grid_points(lat, lon, radius)
        north_lat = points[0][0]
        south_lat = points[(GRID_SIZE - 1) * GRID_SIZE][0]
        span_km = (north_lat - south_lat) * 111.32
        assert abs(span_km - 2 * radius) / (2 * radius) < 0.01  # within 1 %


# ---------------------------------------------------------------------------
# fetch_rain_forecast — parse logic
# ---------------------------------------------------------------------------

class TestFetchRainForecast:
    def _build_payload(self, current_time="2026-09-26T17:15",
                       hourly_times=None, base_precip=None):
        """Build a 25-location payload mimicking the real Open-Meteo response."""
        if hourly_times is None:
            hourly_times = [
                "2026-09-26T17:00",
                "2026-09-26T18:00",
                "2026-09-26T19:00",
                "2026-09-26T20:00",
            ]
        if base_precip is None:
            base_precip = [0.0, 1.0, 2.0, 3.0]
        return [
            _make_location(
                current_time=current_time,
                hourly_times=hourly_times,
                hourly_precip=base_precip,
            )
            for _ in range(25)
        ]

    def test_returns_correct_structure(self):
        payload = self._build_payload()
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)

        assert result["radius_km"] == 25
        assert "step_km" in result
        assert "hours" in result
        assert "cells" in result
        assert "fetched_at" in result

    def test_25_cells(self):
        payload = self._build_payload()
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)
        assert len(result["cells"]) == 25

    def test_strictly_after_current_time(self):
        # current_time=17:15 → keep 18:00, 19:00, 20:00; drop 17:00
        payload = self._build_payload(
            current_time="2026-09-26T17:15",
            hourly_times=[
                "2026-09-26T17:00",
                "2026-09-26T18:00",
                "2026-09-26T19:00",
                "2026-09-26T20:00",
            ],
            base_precip=[9.9, 1.0, 2.0, 3.0],
        )
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)

        assert len(result["hours"]) == 3
        # 17:00 must not appear in hours
        for h in result["hours"]:
            assert "T17:00" not in h
        # 17:00 precip value (9.9) must not appear in any cell
        for cell in result["cells"]:
            assert len(cell["mm"]) == 3
            assert 9.9 not in cell["mm"]

    def test_none_precip_counts_as_zero(self):
        payload = self._build_payload(
            hourly_times=[
                "2026-09-26T17:00",
                "2026-09-26T18:00",
                "2026-09-26T19:00",
                "2026-09-26T20:00",
            ],
            base_precip=[None, None, None, None],
        )
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)

        for cell in result["cells"]:
            assert all(v == 0.0 for v in cell["mm"])

    def test_short_hours_no_crash(self):
        # Only 2 future hours available — should return them without raising.
        payload = self._build_payload(
            current_time="2026-09-26T17:15",
            hourly_times=["2026-09-26T18:00", "2026-09-26T19:00"],
            base_precip=[1.5, 2.5],
        )
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)

        assert len(result["hours"]) == 2
        for cell in result["cells"]:
            assert len(cell["mm"]) == 2

    def test_hours_are_iso_utc_with_offset(self):
        payload = self._build_payload()
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)
        for h in result["hours"]:
            assert h.endswith("+00:00")
            dt = datetime.fromisoformat(h)
            assert dt.tzinfo is not None

    def test_step_km_in_result(self):
        payload = self._build_payload()
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)
        expected_step = round(2 * 25 / (GRID_SIZE - 1), 2)
        assert result["step_km"] == pytest.approx(expected_step)

    def test_wrong_count_raises(self):
        payload = [_make_location()]  # only 1 instead of 25
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            with pytest.raises(ValueError, match="Expected 25"):
                fetch_rain_forecast(13.75, 100.5, 25)

    def test_fetched_at_is_iso_utc(self):
        payload = self._build_payload()
        with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = fetch_rain_forecast(13.75, 100.5, 25)
        from datetime import timezone
        dt = datetime.fromisoformat(result["fetched_at"])
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# get_rain_forecast — settings + cache
# ---------------------------------------------------------------------------

class TestGetRainForecast:
    def test_returns_none_with_blank_lat(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db
        assert get_rain_forecast(25, conn=conn) is None

    def test_returns_none_with_blank_lon(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "")
        conn, _ = tmp_db
        assert get_rain_forecast(25, conn=conn) is None

    def test_returns_none_with_unparseable_coords(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "not_a_float")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db
        assert get_rain_forecast(25, conn=conn) is None

    def _forecast_payload(self):
        hourly_times = [
            "2026-09-26T17:00",
            "2026-09-26T18:00",
            "2026-09-26T19:00",
            "2026-09-26T20:00",
        ]
        loc = _make_location(hourly_times=hourly_times, hourly_precip=[0.0] * 4)
        return [loc] * 25

    def test_cache_second_call_no_http(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db

        payload = self._forecast_payload()
        with patch(
            "wlm.rain_forecast.urllib.request.urlopen",
            return_value=_mock_urlopen(payload),
        ) as mock_open:
            get_rain_forecast(25, conn=conn)
            get_rain_forecast(25, conn=conn)
            assert mock_open.call_count == 1

    def test_cache_expires_after_ttl(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")
        conn, _ = tmp_db

        payload = self._forecast_payload()
        # First call stores ts=0.0.
        # Second call checks: monotonic() returns TTL+1, so TTL+1 − 0.0 > TTL → re-fetch.
        # Third call (second store) gets TTL+1 as well.
        times = [
            0.0,
            rain_forecast_mod._CACHE_TTL_SECONDS + 1,
            rain_forecast_mod._CACHE_TTL_SECONDS + 1,
        ]
        call_idx = [0]

        def fake_monotonic():
            val = times[min(call_idx[0], len(times) - 1)]
            call_idx[0] += 1
            return val

        with patch(
            "wlm.rain_forecast.urllib.request.urlopen",
            return_value=_mock_urlopen(payload),
        ) as mock_open:
            with patch("wlm.rain_forecast.time.monotonic", side_effect=fake_monotonic):
                get_rain_forecast(25, conn=conn)   # first fetch; ts stored via monotonic() → 0.0
                get_rain_forecast(25, conn=conn)   # check monotonic()→TTL+1, expired → re-fetch
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
async def test_rain_forecast_bad_radius(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/rain-forecast?radius=7")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rain_forecast_unauthenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/rain-forecast?radius=25")
        # Unauthenticated → redirect (302) or 401/403
        assert resp.status_code in (302, 401, 403)


@pytest.mark.asyncio
async def test_rain_forecast_enabled_false_blank_coords(app_env, monkeypatch):
    """When lat/lon are blank, endpoint returns {"enabled": false}."""
    monkeypatch.setenv("LATITUDE", "")
    monkeypatch.setenv("LONGITUDE", "")
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/rain-forecast?radius=25")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False}


@pytest.mark.asyncio
async def test_rain_forecast_502_on_fetch_error(app_env, monkeypatch):
    monkeypatch.setenv("LATITUDE", "13.75")
    monkeypatch.setenv("LONGITUDE", "100.5")
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    with patch("wlm.rain_forecast.urllib.request.urlopen", side_effect=Exception("network down")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookies = await get_session_cookie(c)
            c.cookies.update(cookies)
            resp = await c.get("/api/rain-forecast?radius=25")
            assert resp.status_code == 502
            assert "unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rain_forecast_200_shape(app_env, monkeypatch):
    monkeypatch.setenv("LATITUDE", "13.75")
    monkeypatch.setenv("LONGITUDE", "100.5")

    hourly_times = [
        "2026-09-26T17:00",
        "2026-09-26T18:00",
        "2026-09-26T19:00",
        "2026-09-26T20:00",
    ]
    loc = _make_location(hourly_times=hourly_times, hourly_precip=[0.1] * 4)
    payload = [loc] * 25

    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    with patch("wlm.rain_forecast.urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookies = await get_session_cookie(c)
            c.cookies.update(cookies)
            resp = await c.get("/api/rain-forecast?radius=25")

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["radius_km"] == 25
    assert "step_km" in data
    assert "hours" in data
    assert "cells" in data
    assert len(data["cells"]) == 25
    assert "fetched_at" in data
