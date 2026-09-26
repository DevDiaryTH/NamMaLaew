"""Tests for wlm/rain_model.py, rain forecast warning, compute_rain_eta, and app integration."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from wlm import db
from wlm import rain_model

# ---------------------------------------------------------------------------
# Shared fixtures (use the core conftest tmp_db)
# ---------------------------------------------------------------------------

# conftest.py provides tmp_db and _isolate_from_host_env (autouse).
# conftest_web provides its own tmp_db — we use the core one here.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_reading(conn, ts: str, level_index: float | None, status: str = "normal") -> int:
    return db.insert_reading(
        {"ts": ts, "status": status, "level_index": level_index, "model_status": status},
        conn=conn,
    )


def _insert_weather(conn, hour_ts: str, mm: float) -> None:
    db.upsert_weather(hour_ts, mm, conn=conn)


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# build_samples
# ---------------------------------------------------------------------------

class TestBuildSamples:
    def test_basic_sample(self, tmp_db):
        """A weather window of 3 rain hours + before/after readings → 1 sample."""
        H = _utc(2026, 9, 10, 12)
        # rain at H-3, H-2, H-1
        for offset in (3, 2, 1):
            _insert_weather(tmp_db, _iso(H - timedelta(hours=offset)), 2.0)
        # also insert H itself so H exists (needed for forecast, not for samples)
        _insert_weather(tmp_db, _iso(H), 0.0)

        _insert_reading(tmp_db, _iso(H), 30.0)               # before
        _insert_reading(tmp_db, _iso(H + timedelta(hours=2)), 36.0)  # after (horizon=2)

        samples = rain_model.build_samples(tmp_db, horizon_hours=2)
        assert len(samples) >= 1
        # Find our sample: rain=6.0, delta=6.0
        found = [(r, d) for r, d in samples if abs(r - 6.0) < 0.01 and abs(d - 6.0) < 0.01]
        assert found, f"Expected (6.0, 6.0) in {samples}"

    def test_missing_rain_hour_skipped(self, tmp_db):
        """If one of H-3..H-1 is missing, no sample is emitted."""
        H = _utc(2026, 9, 10, 12)
        # Only H-2 and H-1 (missing H-3)
        for offset in (2, 1):
            _insert_weather(tmp_db, _iso(H - timedelta(hours=offset)), 1.0)

        _insert_reading(tmp_db, _iso(H), 30.0)
        _insert_reading(tmp_db, _iso(H + timedelta(hours=2)), 35.0)

        samples = rain_model.build_samples(tmp_db, horizon_hours=2)
        assert samples == []

    def test_missing_reading_skipped(self, tmp_db):
        """If no reading near H or H+horizon, sample is skipped."""
        H = _utc(2026, 9, 10, 12)
        for offset in (3, 2, 1):
            _insert_weather(tmp_db, _iso(H - timedelta(hours=offset)), 1.5)

        # No readings at all
        samples = rain_model.build_samples(tmp_db, horizon_hours=2)
        assert samples == []

    def test_feedback_correction_applied(self, tmp_db):
        """Feedback verdict='wrong' true_level_index replaces raw level."""
        H = _utc(2026, 9, 10, 12)
        for offset in (3, 2, 1):
            _insert_weather(tmp_db, _iso(H - timedelta(hours=offset)), 3.0)
        _insert_weather(tmp_db, _iso(H), 0.0)  # H must be in weather_by_hour

        # Raw reading has wrong level
        rid_before = _insert_reading(tmp_db, _iso(H), 20.0)
        _insert_reading(tmp_db, _iso(H + timedelta(hours=2)), 35.0)

        # Correct the "before" reading via feedback
        tmp_db.execute(
            "INSERT INTO feedback (reading_id, ts, verdict, true_level_index) VALUES (?,?,?,?)",
            (rid_before, _iso(H), "wrong", 25.0),
        )
        tmp_db.commit()

        samples = rain_model.build_samples(tmp_db, horizon_hours=2)
        # delta should be 35.0 - 25.0 = 10.0 (corrected), not 35.0 - 20.0 = 15.0
        found = [(r, d) for r, d in samples if abs(d - 10.0) < 0.01]
        assert found, f"Expected delta 10.0 (corrected), got {samples}"

    def test_null_level_skipped(self, tmp_db):
        """Readings with level_index=NULL are not used."""
        H = _utc(2026, 9, 10, 12)
        for offset in (3, 2, 1):
            _insert_weather(tmp_db, _iso(H - timedelta(hours=offset)), 2.0)

        _insert_reading(tmp_db, _iso(H), None)  # NULL level
        _insert_reading(tmp_db, _iso(H + timedelta(hours=2)), 35.0)

        samples = rain_model.build_samples(tmp_db, horizon_hours=2)
        assert samples == []


# ---------------------------------------------------------------------------
# fit_rain_response
# ---------------------------------------------------------------------------

class TestFitRainResponse:
    def _populate(self, conn, a: float, b: float, n: int = 25):
        """Insert n synthetic samples with known a,b relationship.

        For each sample, weather at H-3, H-2, H-1 provides the rain; H itself is
        also inserted so that build_samples iterates over it and finds the 3 prior hours.
        """
        base = _utc(2026, 1, 1, 0)
        for i in range(n):
            H = base + timedelta(hours=i * 6)
            rain = float(i % 5 + 1)  # 1–5 mm
            for offset in (3, 2, 1):
                db.upsert_weather(_iso(H - timedelta(hours=offset)), rain / 3.0, conn=conn)
            # H must be in weather_by_hour so build_samples iterates it
            db.upsert_weather(_iso(H), 0.0, conn=conn)
            # before level
            level_before = 20.0
            level_after = level_before + a * rain + b
            db._insert("readings", {
                "ts": _iso(H), "status": "normal", "level_index": level_before,
                "model_status": "normal",
            }, conn=conn)
            db._insert("readings", {
                "ts": _iso(H + timedelta(hours=2)), "status": "normal",
                "level_index": level_after, "model_status": "normal",
            }, conn=conn)

    def test_recovers_known_ab(self, tmp_db):
        """Fitting synthetic data with known a,b returns values within 10%."""
        a_true, b_true = 3.0, 0.5
        self._populate(tmp_db, a_true, b_true, n=25)
        result = rain_model.fit_rain_response(tmp_db, min_rain_samples=20)
        assert result is not None, "Expected a fitted model"
        assert abs(result["a"] - a_true) / max(abs(a_true), 0.01) < 0.10, \
            f"a={result['a']} not within 10% of {a_true}"
        assert "b" in result
        assert result["n_rain"] >= 20
        assert result["r2"] >= 0.0

    def test_insufficient_data_returns_none(self, tmp_db):
        """Fewer than min_rain_samples → None."""
        H = _utc(2026, 9, 10, 12)
        # Only 5 rain samples (< 20 required)
        for i in range(5):
            h = H + timedelta(hours=i * 6)
            for offset in (3, 2, 1):
                db.upsert_weather(_iso(h - timedelta(hours=offset)), 1.0, conn=tmp_db)
            _insert_reading(tmp_db, _iso(h), 20.0)
            _insert_reading(tmp_db, _iso(h + timedelta(hours=2)), 23.0)

        result = rain_model.fit_rain_response(tmp_db, min_rain_samples=20)
        assert result is None

    def test_returns_required_keys(self, tmp_db):
        a_true, b_true = 2.0, 1.0
        self._populate(tmp_db, a_true, b_true, n=25)
        result = rain_model.fit_rain_response(tmp_db, min_rain_samples=20)
        assert result is not None
        for key in ("a", "b", "n", "n_rain", "r2", "fitted_at"):
            assert key in result, f"Missing key {key}"


# ---------------------------------------------------------------------------
# maybe_refit
# ---------------------------------------------------------------------------

class TestMaybeRefit:
    def _populate(self, conn, n=25):
        base = _utc(2026, 1, 1, 0)
        for i in range(n):
            H = base + timedelta(hours=i * 6)
            for offset in (3, 2, 1):
                db.upsert_weather(_iso(H - timedelta(hours=offset)), 2.0, conn=conn)
            db.upsert_weather(_iso(H), 0.0, conn=conn)
            db._insert("readings", {
                "ts": _iso(H), "status": "normal", "level_index": 20.0,
                "model_status": "normal",
            }, conn=conn)
            db._insert("readings", {
                "ts": _iso(H + timedelta(hours=2)), "status": "normal",
                "level_index": 26.0, "model_status": "normal",
            }, conn=conn)

    def test_fits_and_stores(self, tmp_db):
        self._populate(tmp_db)
        result = rain_model.maybe_refit(tmp_db)
        assert "a" in result
        stored = db.get_state("rain_model", conn=tmp_db)
        assert stored is not None
        assert json.loads(stored)["a"] == pytest.approx(result["a"])

    def test_throttle_no_refit_within_24h(self, tmp_db):
        self._populate(tmp_db)
        now1 = _utc(2026, 9, 20, 10)
        result1 = rain_model.maybe_refit(tmp_db, now=now1)
        assert "a" in result1

        # Refit 1 hour later — should return cached result, not refit
        now2 = now1 + timedelta(hours=1)
        result2 = rain_model.maybe_refit(tmp_db, now=now2)
        assert result2["fitted_at"] == result1["fitted_at"], "Should not refit within 24h"

    def test_refit_after_24h(self, tmp_db):
        self._populate(tmp_db)
        now1 = _utc(2026, 9, 20, 10)
        result1 = rain_model.maybe_refit(tmp_db, now=now1)

        now2 = now1 + timedelta(hours=25)
        result2 = rain_model.maybe_refit(tmp_db, now=now2)
        assert result2["fitted_at"] != result1["fitted_at"], "Should refit after 24h"

    def test_insufficient_data_stores_status(self, tmp_db):
        # No data → insufficient_data status stored
        result = rain_model.maybe_refit(tmp_db)
        assert result["status"] == "insufficient_data"
        assert "n_rain" in result
        assert "needed" in result


# ---------------------------------------------------------------------------
# load_model
# ---------------------------------------------------------------------------

class TestLoadModel:
    def test_returns_none_when_no_state(self, tmp_db):
        assert rain_model.load_model(tmp_db) is None

    def test_returns_none_for_insufficient_data(self, tmp_db):
        db.set_state("rain_model", json.dumps({
            "status": "insufficient_data", "n_rain": 3, "needed": 20,
            "fitted_at": "2026-09-20T10:00:00+00:00",
        }), conn=tmp_db)
        assert rain_model.load_model(tmp_db) is None

    def test_returns_model_when_fitted(self, tmp_db):
        model = {"a": 2.5, "b": 0.3, "n": 30, "n_rain": 25, "r2": 0.8,
                 "fitted_at": "2026-09-20T10:00:00+00:00"}
        db.set_state("rain_model", json.dumps(model), conn=tmp_db)
        loaded = rain_model.load_model(tmp_db)
        assert loaded is not None
        assert loaded["a"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# predict_rise
# ---------------------------------------------------------------------------

class TestPredictRise:
    def _store_model(self, conn, a=2.0, b=0.5, r2=0.85, n=30, n_rain=25):
        db.set_state("rain_model", json.dumps({
            "a": a, "b": b, "n": n, "n_rain": n_rain, "r2": r2,
            "fitted_at": "2026-09-20T10:00:00+00:00",
        }), conn=conn)

    def test_basic_prediction(self, tmp_db):
        self._store_model(tmp_db, a=3.0, b=0.0)
        _insert_reading(tmp_db, _iso(_utc(2026, 9, 20, 11, 55)), 40.0)

        now = _utc(2026, 9, 20, 12, 0)
        # Insert forecast hours [12, 13, 14]
        for h in range(3):
            db.upsert_weather(_iso(_utc(2026, 9, 20, 12 + h)), 2.0, conn=tmp_db)

        result = rain_model.predict_rise(tmp_db, now=now, horizon_hours=3)
        assert result is not None
        assert result["forecast_rain_mm"] == pytest.approx(6.0)
        assert result["predicted_rise"] == pytest.approx(18.0)  # 3.0*6 + 0
        assert result["current_level"] == pytest.approx(40.0)
        assert result["predicted_level"] == pytest.approx(58.0)

    def test_clamped_rise_not_negative(self, tmp_db):
        """predicted_rise is clamped to 0 when model predicts negative rise."""
        self._store_model(tmp_db, a=-1.0, b=-5.0)
        _insert_reading(tmp_db, _iso(_utc(2026, 9, 20, 11, 55)), 40.0)

        now = _utc(2026, 9, 20, 12, 0)
        db.upsert_weather(_iso(_utc(2026, 9, 20, 12)), 1.0, conn=tmp_db)

        result = rain_model.predict_rise(tmp_db, now=now, horizon_hours=3)
        assert result is not None
        assert result["predicted_rise"] >= 0.0

    def test_clamped_level_not_above_100(self, tmp_db):
        """predicted_level is clamped to 100."""
        self._store_model(tmp_db, a=20.0, b=0.0)
        _insert_reading(tmp_db, _iso(_utc(2026, 9, 20, 11, 55)), 95.0)

        now = _utc(2026, 9, 20, 12, 0)
        for h in range(3):
            db.upsert_weather(_iso(_utc(2026, 9, 20, 12 + h)), 5.0, conn=tmp_db)

        result = rain_model.predict_rise(tmp_db, now=now, horizon_hours=3)
        assert result is not None
        assert result["predicted_level"] <= 100.0

    def test_no_model_returns_none(self, tmp_db):
        _insert_reading(tmp_db, _iso(_utc(2026, 9, 20, 12)), 40.0)
        result = rain_model.predict_rise(tmp_db)
        assert result is None

    def test_no_current_level_returns_none(self, tmp_db):
        self._store_model(tmp_db)
        now = _utc(2026, 9, 20, 12, 0)
        db.upsert_weather(_iso(_utc(2026, 9, 20, 12)), 2.0, conn=tmp_db)
        # No readings
        result = rain_model.predict_rise(tmp_db, now=now)
        assert result is None

    def test_no_forecast_rows_returns_none(self, tmp_db):
        self._store_model(tmp_db)
        _insert_reading(tmp_db, _iso(_utc(2026, 9, 20, 11, 55)), 40.0)
        # Weather rows are in the past, not in [now, now+3h)
        now = _utc(2026, 9, 20, 12, 0)
        db.upsert_weather(_iso(_utc(2026, 9, 20, 9)), 5.0, conn=tmp_db)
        result = rain_model.predict_rise(tmp_db, now=now)
        assert result is None


# ---------------------------------------------------------------------------
# compute_rain_eta
# ---------------------------------------------------------------------------

class TestComputeRainEta:
    def test_eta_when_predicted_above_critical(self):
        from web import metrics
        prediction = {
            "forecast_rain_mm": 10.0,
            "predicted_rise": 20.0,
            "current_level": 70.0,
            "predicted_level": 90.0,
            "n": 30,
            "n_rain": 25,
            "r2": 0.8,
        }
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)

        eta = metrics.compute_rain_eta(conn, level_critical=85.0, prediction=prediction)
        assert eta is not None
        assert eta["rain_based"] is True
        assert "rain forecast" in eta["label"]
        # interpolation: 3 * (85-70)/20 = 2.25h → 2h 15m
        assert eta["hours"] == 2
        assert eta["minutes"] == pytest.approx(15, abs=1)

    def test_returns_none_when_below_critical(self):
        from web import metrics
        prediction = {
            "forecast_rain_mm": 5.0,
            "predicted_rise": 5.0,
            "current_level": 70.0,
            "predicted_level": 75.0,
            "n": 30,
            "n_rain": 25,
            "r2": 0.8,
        }
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)

        eta = metrics.compute_rain_eta(conn, level_critical=85.0, prediction=prediction)
        assert eta is None

    def test_returns_none_when_prediction_is_none(self):
        from web import metrics
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        eta = metrics.compute_rain_eta(conn, level_critical=85.0, prediction=None)
        assert eta is None

    def test_already_critical(self):
        from web import metrics
        prediction = {
            "forecast_rain_mm": 5.0,
            "predicted_rise": 5.0,
            "current_level": 92.0,
            "predicted_level": 97.0,
            "n": 30,
            "n_rain": 25,
            "r2": 0.8,
        }
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        eta = metrics.compute_rain_eta(conn, level_critical=85.0, prediction=prediction)
        assert eta is not None
        assert eta["hours"] == 0 and eta["minutes"] == 0


# ---------------------------------------------------------------------------
# maybe_send_forecast_warning
# ---------------------------------------------------------------------------

class TestForecastWarning:
    def _make_prediction(self, current, predicted, rain=5.0, n_rain=25, r2=0.8):
        return {
            "forecast_rain_mm": rain,
            "predicted_rise": predicted - current,
            "current_level": current,
            "predicted_level": predicted,
            "n": 30,
            "n_rain": n_rain,
            "r2": r2,
        }

    def test_sends_when_crossing_warning(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        db.set_setting("telegram_enabled", "true", conn=tmp_db)

        pred = self._make_prediction(current=60.0, predicted=80.0)
        with patch("wlm.alerts.tg.send_message", return_value=(True, None)) as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=True, conn=tmp_db)
        assert sent is True
        mock_msg.assert_called_once()
        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='forecast'").fetchall()
        assert len(rows) == 1

    def test_not_sent_when_disabled(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        pred = self._make_prediction(current=60.0, predicted=80.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=False, conn=tmp_db)
        assert sent is False
        mock_msg.assert_not_called()

    def test_not_sent_when_not_crossing(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        # predicted_level < level_warning
        pred = self._make_prediction(current=60.0, predicted=65.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=True, conn=tmp_db)
        assert sent is False
        mock_msg.assert_not_called()

    def test_not_sent_when_already_above_warning(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        # current_level already >= level_warning → not a crossing from below
        pred = self._make_prediction(current=75.0, predicted=85.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=True, conn=tmp_db)
        assert sent is False
        mock_msg.assert_not_called()

    def test_throttled_within_3h(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        from wlm.alerts import _FORECAST_WARNING_STATE_KEY
        db.set_setting("telegram_bot_token", "tok", conn=tmp_db)
        db.set_setting("telegram_chat_id", "123", conn=tmp_db)
        db.set_setting("telegram_enabled", "true", conn=tmp_db)

        # Set last sent to 1h ago
        last_sent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        db.set_state(_FORECAST_WARNING_STATE_KEY, last_sent, conn=tmp_db)

        pred = self._make_prediction(current=60.0, predicted=80.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=True, conn=tmp_db)
        assert sent is False
        mock_msg.assert_not_called()

    def test_dry_run_does_not_send(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        pred = self._make_prediction(current=60.0, predicted=80.0)
        with patch("wlm.alerts.tg.send_message") as mock_msg:
            sent = maybe_send_forecast_warning(pred, level_warning=70.0, enabled=True,
                                               dry_run=True, conn=tmp_db)
        assert sent is True
        mock_msg.assert_not_called()
        # dry_run should not insert alert or set throttle state
        rows = tmp_db.execute("SELECT * FROM alerts WHERE kind='forecast'").fetchall()
        assert len(rows) == 0

    def test_prediction_none_returns_false(self, tmp_db):
        from wlm.alerts import maybe_send_forecast_warning
        sent = maybe_send_forecast_warning(None, level_warning=70.0, enabled=True, conn=tmp_db)
        assert sent is False


# ---------------------------------------------------------------------------
# Web app integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def web_fixtures():
    """Import web fixtures without triggering conftest_web autouse isolation issues."""
    from tests.conftest_web import tmp_db as _tmp_db
    return _tmp_db


class TestOverviewWithRainModel:
    """Test that the overview page renders correctly with and without a rain model."""

    def _reload_app(self):
        if "web.app" in sys.modules:
            del sys.modules["web.app"]
        import web.app
        return web.app.app

    @pytest.mark.asyncio
    async def test_overview_without_rain_model(self):
        """Overview page loads (200) when no rain model exists."""
        from tests.conftest_web import tmp_db, app_env, get_session_cookie
        from httpx import AsyncClient, ASGITransport
        import tempfile, os

        tmp_dir = tempfile.mkdtemp()
        db_file = Path(tmp_dir) / "test.db"
        snap_dir = Path(tmp_dir) / "snapshots"
        snap_dir.mkdir()
        conn = db.connect(db_file)

        os.environ["WLM_DB"] = str(db_file)
        os.environ["WLM_SNAPSHOT_DIR"] = str(snap_dir)
        os.environ["DASHBOARD_PASSWORD"] = "testpass"

        try:
            app = self._reload_app()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                cookies = await get_session_cookie(c)
                c.cookies.update(cookies)
                resp = await c.get("/")
                assert resp.status_code == 200
                # Should show "no rain model" message or learning message
                # Either way, page must load without error
        finally:
            conn.close()
            for var in ("WLM_DB", "WLM_SNAPSHOT_DIR", "DASHBOARD_PASSWORD"):
                os.environ.pop(var, None)

    @pytest.mark.asyncio
    async def test_api_summary_includes_rain_fields(self):
        """GET /api/summary returns rain_prediction and rain_eta keys."""
        from tests.conftest_web import get_session_cookie
        from httpx import AsyncClient, ASGITransport
        import tempfile, os

        tmp_dir = tempfile.mkdtemp()
        db_file = Path(tmp_dir) / "test.db"
        snap_dir = Path(tmp_dir) / "snapshots"
        snap_dir.mkdir()
        conn = db.connect(db_file)

        os.environ["WLM_DB"] = str(db_file)
        os.environ["WLM_SNAPSHOT_DIR"] = str(snap_dir)
        os.environ["DASHBOARD_PASSWORD"] = "testpass"

        try:
            app = self._reload_app()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                cookies = await get_session_cookie(c)
                c.cookies.update(cookies)
                resp = await c.get("/api/summary")
                assert resp.status_code == 200
                data = resp.json()
                # Additive fields must exist (may be null)
                assert "rain_prediction" in data
                assert "rain_eta" in data
        finally:
            conn.close()
            for var in ("WLM_DB", "WLM_SNAPSHOT_DIR", "DASHBOARD_PASSWORD"):
                os.environ.pop(var, None)

    @pytest.mark.asyncio
    async def test_api_summary_with_rain_model(self):
        """GET /api/summary returns rain_prediction data when model is fitted and forecast exists."""
        from tests.conftest_web import get_session_cookie
        from httpx import AsyncClient, ASGITransport
        import tempfile, os

        tmp_dir = tempfile.mkdtemp()
        db_file = Path(tmp_dir) / "test.db"
        snap_dir = Path(tmp_dir) / "snapshots"
        snap_dir.mkdir()
        conn = db.connect(db_file)

        # Store a fitted model
        db.set_state("rain_model", json.dumps({
            "a": 2.0, "b": 0.5, "n": 30, "n_rain": 25, "r2": 0.8,
            "fitted_at": "2026-09-20T10:00:00+00:00",
        }), conn=conn)

        # Insert current reading
        now = datetime.now(timezone.utc)
        db._insert("readings", {
            "ts": now.isoformat(timespec="seconds"),
            "status": "normal",
            "level_index": 40.0,
            "model_status": "normal",
        }, conn=conn)

        # Insert forecast weather hours — use h+1..h+3 so all 3 are after the current
        # partial hour and guaranteed to fall within [now, now+3h).
        h_floor = now.replace(minute=0, second=0, microsecond=0)
        for h in range(1, 4):
            h_dt = h_floor + timedelta(hours=h)
            db.upsert_weather(h_dt.isoformat(timespec="seconds"), 3.0, conn=conn)

        conn.close()

        os.environ["WLM_DB"] = str(db_file)
        os.environ["WLM_SNAPSHOT_DIR"] = str(snap_dir)
        os.environ["DASHBOARD_PASSWORD"] = "testpass"

        try:
            app = self._reload_app()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                cookies = await get_session_cookie(c)
                c.cookies.update(cookies)
                resp = await c.get("/api/summary")
                assert resp.status_code == 200
                data = resp.json()
                assert data["rain_prediction"] is not None
                assert data["rain_prediction"]["forecast_rain_mm"] == pytest.approx(9.0)
        finally:
            for var in ("WLM_DB", "WLM_SNAPSHOT_DIR", "DASHBOARD_PASSWORD"):
                os.environ.pop(var, None)
