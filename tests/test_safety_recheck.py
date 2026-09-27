"""Tests for the safety re-check helpers and runner integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

import pytest
from PIL import Image

from wlm import db, learning
from wlm.learning import (
    SAFETY_DROP_LEVEL,
    SAFETY_PREV_WINDOW_MINUTES,
    needs_safety_recheck,
    pick_higher,
    previous_reading,
)
from wlm.runner import run_cycle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_image(path: Path) -> None:
    img = Image.new("RGB", (100, 100), (100, 100, 100))
    img.save(str(path), "JPEG")


def _seed_reading(conn, status="normal", level_index=25.0, ts=None):
    return db.insert_reading(
        {
            "ts": ts or db.now_iso(),
            "status": status,
            "model_status": status,
            "level_index": level_index,
            "confidence": 0.9,
            "description": "Test",
            "reason": "Test",
            "distance_to_critical": "far",
            "composite_path": None,
            "model": "claude-test",
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_usd": 0.0001,
            "capture_ms": 500,
            "analysis_ms": 2000,
            "error": None,
        },
        conn=conn,
    )


def _analysis_result(status, level, in_tok=100, out_tok=20, cost=0.001):
    return (
        {
            "level_status": status,
            "level_index": level,
            "estimated_level_description": "test",
            "distance_to_critical": "unknown",
            "confidence": 0.9,
            "reason": "test",
            "per_lens": [],
        },
        in_tok,
        out_tok,
        cost,
    )


# ---------------------------------------------------------------------------
# needs_safety_recheck truth table
# ---------------------------------------------------------------------------

class TestNeedsSafetyRecheck:
    def test_status_drop_triggers(self):
        prev = {"status": "warning", "level_index": 60.0}
        assert needs_safety_recheck(prev, "normal", 20.0, used_examples=False) is True

    def test_status_rise_no_trigger(self):
        prev = {"status": "normal", "level_index": 20.0}
        assert needs_safety_recheck(prev, "warning", 60.0, used_examples=False) is False

    def test_same_status_no_trigger(self):
        prev = {"status": "warning", "level_index": 60.0}
        assert needs_safety_recheck(prev, "warning", 60.0, used_examples=False) is False

    def test_level_drop_exactly_threshold_triggers(self):
        prev = {"status": "warning", "level_index": 72.0}
        # 72 - 42 = 30 → triggers
        assert needs_safety_recheck(prev, "warning", 42.0, used_examples=False) is True

    def test_level_drop_below_threshold_no_trigger(self):
        prev = {"status": "warning", "level_index": 72.0}
        # 72 - 43 = 29 → does not trigger
        assert needs_safety_recheck(prev, "warning", 43.0, used_examples=False) is False

    def test_level_rise_no_trigger(self):
        prev = {"status": "normal", "level_index": 20.0}
        assert needs_safety_recheck(prev, "normal", 50.0, used_examples=False) is False

    def test_none_prev_with_examples_triggers(self):
        assert needs_safety_recheck(None, "normal", 20.0, used_examples=True) is True

    def test_none_prev_without_examples_no_trigger(self):
        assert needs_safety_recheck(None, "normal", 20.0, used_examples=False) is False

    def test_none_level_indices_status_drop_triggers(self):
        prev = {"status": "warning", "level_index": None}
        # status drops: warning → normal
        assert needs_safety_recheck(prev, "normal", None, used_examples=False) is True

    def test_none_level_indices_same_status_no_trigger(self):
        prev = {"status": "normal", "level_index": None}
        assert needs_safety_recheck(prev, "normal", None, used_examples=False) is False

    def test_prev_none_level_new_has_level_no_trigger_by_level(self):
        # prev_level is None → level comparison skipped; only status matters
        prev = {"status": "warning", "level_index": None}
        # same status rank → no trigger
        assert needs_safety_recheck(prev, "warning", 10.0, used_examples=False) is False


# ---------------------------------------------------------------------------
# pick_higher
# ---------------------------------------------------------------------------

class TestPickHigher:
    def test_higher_rank_wins(self):
        assert pick_higher(("warning", 60.0), ("normal", 80.0)) == "a"

    def test_lower_rank_loses(self):
        assert pick_higher(("normal", 80.0), ("warning", 60.0)) == "b"

    def test_same_rank_higher_level_wins(self):
        assert pick_higher(("normal", 30.0), ("normal", 50.0)) == "b"

    def test_same_rank_lower_level_loses(self):
        assert pick_higher(("normal", 50.0), ("normal", 30.0)) == "a"

    def test_full_tie_returns_a(self):
        assert pick_higher(("normal", 30.0), ("normal", 30.0)) == "a"

    def test_none_level_counts_as_minus_one(self):
        assert pick_higher(("normal", None), ("normal", 0.0)) == "b"
        assert pick_higher(("normal", 0.0), ("normal", None)) == "a"

    def test_unknown_loses_to_known(self):
        assert pick_higher(("unknown", 99.0), ("normal", 1.0)) == "b"
        assert pick_higher(("normal", 1.0), ("unknown", 99.0)) == "a"

    def test_critical_beats_warning(self):
        assert pick_higher(("critical", 5.0), ("warning", 90.0)) == "a"

    def test_both_unknown_returns_a(self):
        assert pick_higher(("unknown", None), ("unknown", None)) == "a"


# ---------------------------------------------------------------------------
# previous_reading
# ---------------------------------------------------------------------------

class TestPreviousReading:
    def test_returns_latest_non_unknown_in_window(self, tmp_db):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=70.0, ts=ts)

        result = previous_reading(conn=tmp_db, now=now)
        assert result is not None
        assert result["status"] == "warning"
        assert result["level_index"] == pytest.approx(70.0)

    def test_excludes_unknown_status(self, tmp_db):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="unknown", level_index=None, ts=ts)

        result = previous_reading(conn=tmp_db, now=now)
        assert result is None

    def test_excludes_readings_outside_window(self, tmp_db):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=SAFETY_PREV_WINDOW_MINUTES + 5)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=70.0, ts=ts)

        result = previous_reading(conn=tmp_db, now=now, window_minutes=SAFETY_PREV_WINDOW_MINUTES)
        assert result is None

    def test_returns_most_recent_when_multiple(self, tmp_db):
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(minutes=50)).isoformat(timespec="seconds")
        ts_new = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="normal", level_index=20.0, ts=ts_old)
        _seed_reading(tmp_db, status="warning", level_index=65.0, ts=ts_new)

        result = previous_reading(conn=tmp_db, now=now)
        assert result["status"] == "warning"

    def test_returns_none_when_no_readings(self, tmp_db):
        result = previous_reading(conn=tmp_db)
        assert result is None


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------

class TestRunnerSafetyRecheck:
    """Integration tests: run_cycle with mocked analyze_images."""

    def test_learned_drop_triggers_second_call_and_plain_result_wins(
        self, tmp_db, tmp_path, monkeypatch
    ):
        """Learned normal/20 after prev warning/72 → second call, plain warning/72 kept."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        # Seed a previous reading inside the window
        now = datetime.now(timezone.utc)
        ts_prev = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=72.0, ts=ts_prev)

        learned_result = _analysis_result("normal", 20.0, in_tok=100, out_tok=20, cost=0.001)
        plain_result = _analysis_result("warning", 72.0, in_tok=80, out_tok=15, cost=0.0008)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.side_effect = [learned_result, plain_result]
            reading_id = run_cycle(dry_run=True, images=[("street", street)])

        assert reading_id is not None
        # Second call must have been made with no examples and no history_text
        assert mock_analyze.call_count == 2
        second_call_kwargs = mock_analyze.call_args_list[1][1]
        assert second_call_kwargs.get("examples") is None
        assert second_call_kwargs.get("history_text") is None

        conn = db.connect()
        reading = conn.execute("SELECT status, level_index FROM readings WHERE id=?",
                               (reading_id,)).fetchone()
        conn.close()
        assert reading["status"] == "warning"
        assert reading["level_index"] == pytest.approx(72.0)

    def test_learned_result_higher_no_second_call(self, tmp_db, tmp_path):
        """Learned result is higher than prev → no second call."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        now = datetime.now(timezone.utc)
        ts_prev = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="normal", level_index=20.0, ts=ts_prev)

        learned_result = _analysis_result("warning", 65.0)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = learned_result
            run_cycle(dry_run=True, images=[("street", street)])

        assert mock_analyze.call_count == 1

    def test_no_learning_context_no_second_call_on_drop(self, tmp_db, tmp_path, monkeypatch):
        """No examples and no history → no second call even on a status drop."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        now = datetime.now(timezone.utc)
        ts_prev = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=72.0, ts=ts_prev)

        # Disable history so no learning context is used
        monkeypatch.setenv("LEARNING_USE_HISTORY", "0")
        db.set_setting("learning_use_history", "0", conn=tmp_db)
        db.set_setting("learning_max_examples", "0", conn=tmp_db)

        dropped_result = _analysis_result("normal", 20.0)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = dropped_result
            run_cycle(dry_run=True, images=[("street", street)])

        assert mock_analyze.call_count == 1

    def test_second_call_raises_first_result_kept(self, tmp_db, tmp_path):
        """If the second call raises, the first result is kept and the cycle still stores a reading."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        now = datetime.now(timezone.utc)
        ts_prev = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=72.0, ts=ts_prev)

        learned_result = _analysis_result("normal", 20.0)

        def side_effect(*args, **kwargs):
            if side_effect.call_count == 0:
                side_effect.call_count += 1
                return learned_result
            raise RuntimeError("second call exploded")

        side_effect.call_count = 0

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.side_effect = side_effect
            reading_id = run_cycle(dry_run=True, images=[("street", street)])

        assert reading_id is not None
        conn = db.connect()
        reading = conn.execute("SELECT status FROM readings WHERE id=?",
                               (reading_id,)).fetchone()
        conn.close()
        # First result (normal) must be stored when second call failed
        assert reading["status"] == "normal"

    def test_tokens_are_summed_when_recheck_runs(self, tmp_db, tmp_path):
        """input_tokens, output_tokens and cost_usd from both calls are summed."""
        street = tmp_path / "street.jpg"
        _make_image(street)

        now = datetime.now(timezone.utc)
        ts_prev = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
        _seed_reading(tmp_db, status="warning", level_index=72.0, ts=ts_prev)

        learned_result = _analysis_result("normal", 20.0, in_tok=100, out_tok=20, cost=0.001)
        plain_result = _analysis_result("warning", 72.0, in_tok=80, out_tok=15, cost=0.0008)

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.side_effect = [learned_result, plain_result]
            reading_id = run_cycle(dry_run=True, images=[("street", street)])

        assert reading_id is not None
        conn = db.connect()
        reading = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM readings WHERE id=?",
            (reading_id,),
        ).fetchone()
        conn.close()
        assert reading["input_tokens"] == 180   # 100 + 80
        assert reading["output_tokens"] == 35   # 20 + 15
        assert reading["cost_usd"] == pytest.approx(0.0018)  # 0.001 + 0.0008
