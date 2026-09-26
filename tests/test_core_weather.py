"""Tests: weather parsing, throttling."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest

from wlm import db
from wlm.weather import refresh_weather


def _mock_urlopen(payload: dict):
    """Return a context manager that yields a response-like object."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


SAMPLE_DATA = {
    "hourly": {
        "time": [
            "2026-09-25T12:00",
            "2026-09-25T13:00",
            "2026-09-25T14:00",
        ],
        "precipitation": [0.0, 2.5, 1.1],
    }
}


class TestWeather:
    def test_parses_and_upserts_hours(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")

        with patch("wlm.weather.urllib.request.urlopen", return_value=_mock_urlopen(SAMPLE_DATA)):
            refresh_weather(conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM weather ORDER BY hour_ts").fetchall()
        assert len(rows) == 3
        assert rows[1]["precipitation_mm"] == pytest.approx(2.5)

    def test_throttled_after_30min(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")

        # Set last fetch to 5 minutes ago
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.set_state("last_weather_fetch_ts", recent, conn=tmp_db)

        with patch("wlm.weather.urllib.request.urlopen") as mock_open:
            refresh_weather(conn=tmp_db)
            mock_open.assert_not_called()

    def test_fetches_after_30min(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")

        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
        db.set_state("last_weather_fetch_ts", old_ts, conn=tmp_db)

        with patch("wlm.weather.urllib.request.urlopen", return_value=_mock_urlopen(SAMPLE_DATA)):
            refresh_weather(conn=tmp_db)

        rows = tmp_db.execute("SELECT * FROM weather").fetchall()
        assert len(rows) == 3

    def test_no_location_skips(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "")
        monkeypatch.setenv("LONGITUDE", "")

        with patch("wlm.weather.urllib.request.urlopen") as mock_open:
            refresh_weather(conn=tmp_db)
            mock_open.assert_not_called()

    def test_failure_does_not_raise(self, tmp_db, monkeypatch):
        monkeypatch.setenv("LATITUDE", "13.75")
        monkeypatch.setenv("LONGITUDE", "100.5")

        with patch("wlm.weather.urllib.request.urlopen", side_effect=Exception("network down")):
            # Should not raise
            refresh_weather(conn=tmp_db)
