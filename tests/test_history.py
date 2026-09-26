"""Tests for recent_context (learning.py) and history integration in analysis + runner."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from wlm import db, learning


# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------

def _make_jpeg(path: Path, w: int = 10, h: int = 10) -> None:
    img = Image.new("RGB", (w, h), (80, 100, 120))
    img.save(str(path), "JPEG")


def _seed_reading(
    conn,
    status: str = "normal",
    level_index: float | None = 25.0,
    confidence: float = 0.85,
    ts: str | None = None,
) -> int:
    return db.insert_reading(
        {
            "ts": ts or db.now_iso(),
            "status": status,
            "model_status": status,
            "level_index": level_index,
            "confidence": confidence,
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


def _seed_weather(conn, hour_ts: str, precipitation_mm: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO weather (hour_ts, precipitation_mm, fetched_at) VALUES (?, ?, ?)",
        (hour_ts, precipitation_mm, db.now_iso()),
    )
    conn.commit()


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """Fresh DB in a temp directory."""
    db_file = tmp_path / "test.db"
    snap = tmp_path / "snapshots"
    snap.mkdir()
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap))
    conn = db.connect(db_file)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# recent_context — basic formatting
# ---------------------------------------------------------------------------

class TestRecentContextFormatting:
    def test_single_reading_format(self, db_conn):
        now = datetime(2026, 9, 27, 10, 30, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", level_index=30.0, confidence=0.9, ts=ts)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "10:15 UTC (-15m ago)" in result
        assert "status=normal" in result
        assert "level=30.0" in result
        assert "conf=90%" in result

    def test_level_index_none_shown_as_dash(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        # Insert a reading manually with NULL level_index (status='unknown' is excluded,
        # so use warning with no level_index)
        db_conn.execute(
            """INSERT INTO readings
               (ts, status, model_status, level_index, confidence, description, reason,
                distance_to_critical, composite_path, model, input_tokens, output_tokens,
                cost_usd, capture_ms, analysis_ms)
               VALUES (?, ?, ?, NULL, 0.5, 'x', 'x', 'x', NULL, 'test', 0, 0, 0, 0, 0)""",
            (ts, "warning", "warning"),
        )
        db_conn.commit()

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "level=-" in result

    def test_chronological_order_newest_last(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts_old = (now - timedelta(minutes=50)).isoformat(timespec="seconds")
        ts_new = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", level_index=10.0, ts=ts_old)
        _seed_reading(db_conn, status="warning", level_index=60.0, ts=ts_new)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        lines = [l for l in result.split("\n") if "status=" in l]
        assert len(lines) == 2
        assert "status=normal" in lines[0]   # older reading comes first
        assert "status=warning" in lines[1]  # newer reading comes last


# ---------------------------------------------------------------------------
# recent_context — window and n limits
# ---------------------------------------------------------------------------

class TestRecentContextWindowAndN:
    def test_readings_outside_window_excluded(self, db_conn):
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        # 3 hours ago — outside 120-minute window
        ts_old = (now - timedelta(hours=3)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", level_index=10.0, ts=ts_old)

        result = learning.recent_context(conn=db_conn, now=now, window_minutes=120)

        assert result is None

    def test_readings_inside_window_included(self, db_conn):
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", level_index=20.0, ts=ts)

        result = learning.recent_context(conn=db_conn, now=now, window_minutes=120)

        assert result is not None
        assert "status=normal" in result

    def test_n_limits_number_of_readings(self, db_conn):
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(8):
            ts = (now - timedelta(minutes=10 * (i + 1))).isoformat(timespec="seconds")
            _seed_reading(db_conn, status="normal", level_index=float(i * 5), ts=ts)

        result = learning.recent_context(conn=db_conn, now=now, n=4)

        assert result is not None
        reading_lines = [l for l in result.split("\n") if "status=" in l]
        assert len(reading_lines) == 4

    def test_returns_none_with_no_readings_no_rain(self, db_conn):
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        result = learning.recent_context(conn=db_conn, now=now)
        assert result is None


# ---------------------------------------------------------------------------
# recent_context — unknown status excluded
# ---------------------------------------------------------------------------

class TestRecentContextUnknownExcluded:
    def test_unknown_readings_not_shown(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="unknown", level_index=None, ts=ts)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is None

    def test_mix_unknown_and_known(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts_unknown = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
        ts_normal = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="unknown", ts=ts_unknown)
        _seed_reading(db_conn, status="normal", level_index=25.0, ts=ts_normal)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        reading_lines = [l for l in result.split("\n") if "status=" in l]
        assert len(reading_lines) == 1
        assert "status=normal" in reading_lines[0]


# ---------------------------------------------------------------------------
# recent_context — feedback annotations
# ---------------------------------------------------------------------------

class TestRecentContextFeedback:
    def test_wrong_feedback_replaces_values(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        rid = _seed_reading(db_conn, status="normal", level_index=25.0, ts=ts)
        learning.record_feedback(
            rid, "wrong", true_status="warning", true_level_index=65.0, conn=db_conn
        )

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "status=warning" in result   # corrected value
        assert "level=65.0" in result       # corrected level
        assert "(human-corrected)" in result
        assert "status=normal" not in result  # original suppressed

    def test_correct_feedback_marked(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        rid = _seed_reading(db_conn, status="normal", level_index=25.0, ts=ts)
        learning.record_feedback(rid, "correct", conn=db_conn)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "(human-confirmed)" in result

    def test_no_feedback_no_annotation(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", level_index=25.0, ts=ts)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "human" not in result


# ---------------------------------------------------------------------------
# recent_context — rain sum
# ---------------------------------------------------------------------------

class TestRecentContextRain:
    def test_rain_sum_included(self, db_conn):
        now = datetime(2026, 9, 27, 15, 0, 0, tzinfo=timezone.utc)
        # Three hours: 12:00, 13:00, 14:00 UTC
        for h in (12, 13, 14):
            hour_ts = f"2026-09-27T{h:02d}:00:00+00:00"
            _seed_weather(db_conn, hour_ts, 2.5)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "Rain in last 3h:" in result
        assert "7.5 mm" in result

    def test_rain_outside_3h_not_counted(self, db_conn):
        now = datetime(2026, 9, 27, 15, 0, 0, tzinfo=timezone.utc)
        # 5 hours ago — outside 3h window
        hour_ts = "2026-09-27T10:00:00+00:00"
        _seed_weather(db_conn, hour_ts, 10.0)

        # Also seed a normal reading so the result isn't None from empty readings
        ts = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", ts=ts)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "Rain in last 3h:" not in result

    def test_rain_only_returns_non_none(self, db_conn):
        now = datetime(2026, 9, 27, 15, 0, 0, tzinfo=timezone.utc)
        hour_ts = "2026-09-27T14:00:00+00:00"
        _seed_weather(db_conn, hour_ts, 5.0)

        # No readings — only rain
        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "Rain in last 3h: 5.0 mm" in result

    def test_no_weather_no_rain_line(self, db_conn):
        now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        _seed_reading(db_conn, status="normal", ts=ts)

        result = learning.recent_context(conn=db_conn, now=now)

        assert result is not None
        assert "Rain" not in result


# ---------------------------------------------------------------------------
# analyze_images — payload ordering with history
# ---------------------------------------------------------------------------

def _good_structured() -> dict:
    return {
        "level_status": "normal",
        "level_index": 25.0,
        "estimated_level_description": "dry",
        "distance_to_critical": "far",
        "confidence": 0.95,
        "reason": "no water",
        "per_lens": [
            {
                "label": "street",
                "observation": "dry",
                "water_coverage_pct": 0.0,
                "line_position": "no_lines",
            }
        ],
    }


def _make_proc_result(structured_output=None) -> subprocess.CompletedProcess:
    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": structured_output,
        "total_cost_usd": 0.001,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(result_event), stderr=""
    )


def _parse_content(mock_run) -> list[dict]:
    call_kwargs = mock_run.call_args
    stdin = (call_kwargs.kwargs or {}).get("input") or (call_kwargs[1] or {}).get("input", "")
    payload = json.loads(stdin)
    return payload["message"]["content"]


class TestAnalyzeImagesHistoryBlocks:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.frame = self.tmp_dir / "street.jpg"
        _make_jpeg(self.frame)
        self.example_img = self.tmp_dir / "example_cam.jpg"
        _make_jpeg(self.example_img)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _call(self, examples=None, history_text=None):
        from wlm.analysis import analyze_images
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images(
                [("street", self.frame)],
                examples=examples,
                history_text=history_text,
            )
        return _parse_content(mock_run)

    def test_no_history_no_marker(self):
        content = self._call(examples=None, history_text=None)
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert not any("Recent readings at this site" in t for t in texts)
        assert not any("Current images to analyze" in t for t in texts)

    def test_history_only_shows_marker_and_history(self):
        content = self._call(history_text="  09:45 UTC (-15m ago)  status=normal  level=25.0  conf=90%")
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert any("Recent readings at this site" in t for t in texts)
        assert any("Current images to analyze" in t for t in texts)

    def test_history_block_before_current_images_marker(self):
        history = "  09:45 UTC (-15m ago)  status=normal  level=25.0  conf=90%"
        content = self._call(history_text=history)
        texts = [b["text"] for b in content if b["type"] == "text"]
        history_idx = next(i for i, t in enumerate(texts) if "Recent readings at this site" in t)
        marker_idx = next(i for i, t in enumerate(texts) if "Current images to analyze" in t)
        assert history_idx < marker_idx

    def test_examples_then_history_then_marker(self):
        examples = [{
            "reading_id": 1,
            "true_status": "critical",
            "true_level_index": 95.0,
            "note": None,
            "images": [("cam", self.example_img)],
            "is_night": False,
        }]
        history = "  09:45 UTC (-15m ago)  status=normal  level=25.0  conf=90%"
        content = self._call(examples=examples, history_text=history)
        texts = [b["text"] for b in content if b["type"] == "text"]

        examples_idx = next(i for i, t in enumerate(texts) if "Verified reference examples" in t)
        history_idx = next(i for i, t in enumerate(texts) if "Recent readings at this site" in t)
        marker_idx = next(i for i, t in enumerate(texts) if "Current images to analyze" in t)
        assert examples_idx < history_idx < marker_idx

    def test_examples_no_history_marker_still_appears(self):
        examples = [{
            "reading_id": 2,
            "true_status": "normal",
            "true_level_index": 10.0,
            "note": None,
            "images": [("cam", self.example_img)],
            "is_night": False,
        }]
        content = self._call(examples=examples, history_text=None)
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert any("Current images to analyze" in t for t in texts)
        assert not any("Recent readings at this site" in t for t in texts)

    def test_prompt_mentions_history_guidance(self):
        history = "  09:45 UTC (-15m ago)  status=normal  level=25.0  conf=90%"
        content = self._call(history_text=history)
        texts = [b["text"] for b in content if b["type"] == "text"]
        prompt = texts[-1]
        assert "recent" in prompt.lower() or "history" in prompt.lower()

    def test_prompt_no_history_guidance_when_no_history(self):
        content = self._call(history_text=None)
        texts = [b["text"] for b in content if b["type"] == "text"]
        prompt = texts[-1]
        # "Recent history:" paragraph should NOT be in the prompt
        assert "Recent history:" not in prompt


# ---------------------------------------------------------------------------
# Runner: learning_use_history=off → history_text=None passed to analyze_images
# ---------------------------------------------------------------------------

class TestRunnerHistorySetting:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.street = self.tmp_dir / "street.jpg"
        _make_jpeg(self.street)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _good_result(self):
        return (
            {
                "level_status": "normal",
                "level_index": 10.0,
                "estimated_level_description": "dry",
                "distance_to_critical": "far",
                "confidence": 0.9,
                "reason": "dry",
                "per_lens": [
                    {
                        "label": "street",
                        "observation": "dry",
                        "water_coverage_pct": 0.0,
                        "line_position": "no_lines",
                    }
                ],
            },
            100, 50, 0.001,
        )

    def test_setting_off_passes_none_to_analyze_images(self, tmp_db, monkeypatch):
        from wlm import runner, db as wlm_db
        wlm_db.set_setting("learning_use_history", "0", conn=tmp_db)

        called_with = {}

        def fake_analyze(frames, **kwargs):
            called_with.update(kwargs)
            return self._good_result()

        with patch("wlm.runner.analyze_images", side_effect=fake_analyze), \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"), \
             patch("wlm.learning.recent_context") as mock_ctx:
            runner.run_cycle(images=[("street", self.street)])

        # recent_context should not have been called at all
        mock_ctx.assert_not_called()
        assert called_with.get("history_text") is None

    def test_setting_on_passes_history_to_analyze_images(self, tmp_db, monkeypatch):
        from wlm import runner, db as wlm_db
        wlm_db.set_setting("learning_use_history", "1", conn=tmp_db)

        called_with = {}
        fake_history = "  10:00 UTC (-5m ago)  status=normal  level=25.0  conf=85%"

        def fake_analyze(frames, **kwargs):
            called_with.update(kwargs)
            return self._good_result()

        with patch("wlm.runner.analyze_images", side_effect=fake_analyze), \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"), \
             patch("wlm.learning.recent_context", return_value=fake_history):
            runner.run_cycle(images=[("street", self.street)])

        assert called_with.get("history_text") == fake_history

    def test_history_context_exception_falls_back_to_none(self, tmp_db, monkeypatch):
        from wlm import runner, db as wlm_db
        wlm_db.set_setting("learning_use_history", "1", conn=tmp_db)

        called_with = {}

        def fake_analyze(frames, **kwargs):
            called_with.update(kwargs)
            return self._good_result()

        with patch("wlm.runner.analyze_images", side_effect=fake_analyze), \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"), \
             patch("wlm.learning.recent_context", side_effect=RuntimeError("db exploded")):
            runner.run_cycle(images=[("street", self.street)])

        assert called_with.get("history_text") is None
