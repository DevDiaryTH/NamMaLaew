"""Tests for wlm.learning: feedback recording, example promotion, selection, and analysis integration."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from wlm import db, learning

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jpeg(path: Path, w: int = 10, h: int = 10) -> None:
    img = Image.new("RGB", (w, h), (80, 100, 120))
    img.save(str(path), "JPEG")


def _seed_reading(conn, status="normal", level_index=25.0) -> int:
    return db.insert_reading({
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
    }, conn=conn)


def _seed_lens_reading(conn, reading_id: int, label: str = "street",
                       snapshot_path: str | None = None, is_night: int = 0) -> None:
    db.insert_lens_reading({
        "reading_id": reading_id,
        "label": label,
        "snapshot_path": snapshot_path,
        "ok": 1,
        "observation": "ok",
        "water_coverage_pct": 5.0,
        "brightness": 120.0,
        "sharpness": 80.0,
        "is_night": is_night,
        "error": None,
        "line_position": "no_lines",
    }, conn=conn)


# ---------------------------------------------------------------------------
# A combined fixture providing both a DB connection and a snapshot directory
# ---------------------------------------------------------------------------

@pytest.fixture
def db_with_snap(tmp_path, monkeypatch):
    """Fresh DB + snapshot directory, both pointing at tmp_path."""
    db_file = tmp_path / "test.db"
    snap = tmp_path / "snapshots"
    snap.mkdir()
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap))
    conn = db.connect(db_file)
    yield conn, snap
    conn.close()


# ---------------------------------------------------------------------------
# record_feedback
# ---------------------------------------------------------------------------

class TestRecordFeedback:
    def test_correct_verdict_defaults_to_reading_values(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn, status="normal", level_index=30.0)

        learning.record_feedback(rid, "correct", conn=conn)

        fb = learning.get_feedback(rid, conn=conn)
        assert fb is not None
        assert fb["verdict"] == "correct"
        assert fb["true_status"] == "normal"
        assert fb["true_level_index"] == pytest.approx(30.0)
        assert fb["is_example"] == 0

    def test_wrong_verdict_requires_true_status(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        with pytest.raises(ValueError, match="true_status is required"):
            learning.record_feedback(rid, "wrong", conn=conn)

    def test_wrong_verdict_with_true_status(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn, status="normal")
        learning.record_feedback(rid, "wrong", true_status="critical",
                                 true_level_index=95.0, conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        assert fb["verdict"] == "wrong"
        assert fb["true_status"] == "critical"
        assert fb["true_level_index"] == pytest.approx(95.0)

    def test_invalid_verdict_raises(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        with pytest.raises(ValueError, match="verdict must be one of"):
            learning.record_feedback(rid, "maybe", conn=conn)

    def test_invalid_true_status_raises(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        with pytest.raises(ValueError, match="true_status must be one of"):
            learning.record_feedback(rid, "correct", true_status="flood", conn=conn)

    def test_level_index_clamped_high(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        learning.record_feedback(rid, "correct", true_level_index=150.0, conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        assert fb["true_level_index"] == pytest.approx(100.0)

    def test_level_index_clamped_low(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        learning.record_feedback(rid, "correct", true_level_index=-10.0, conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        assert fb["true_level_index"] == pytest.approx(0.0)

    def test_upsert_overwrites_previous_verdict(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        learning.record_feedback(rid, "correct", conn=conn)
        learning.record_feedback(rid, "wrong", true_status="warning", conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        assert fb["verdict"] == "wrong"
        assert fb["true_status"] == "warning"

    def test_unknown_reading_raises(self, db_with_snap):
        conn, snap = db_with_snap
        with pytest.raises(ValueError, match="No reading with id=9999"):
            learning.record_feedback(9999, "correct", conn=conn)

    def test_note_stored(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        learning.record_feedback(rid, "correct", note="very clear image", conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        assert fb["note"] == "very clear image"


# ---------------------------------------------------------------------------
# Example promotion: as_example copies images
# ---------------------------------------------------------------------------

class TestExamplePromotion:
    def test_example_copies_raw_image_when_no_overlay(self, db_with_snap):
        conn, snap = db_with_snap
        raw = snap / "street_snap.jpg"
        _make_jpeg(raw)
        rid = _seed_reading(conn)
        _seed_lens_reading(conn, rid, label="street", snapshot_path="street_snap.jpg")

        learning.record_feedback(rid, "correct", as_example=True, conn=conn)

        fb = learning.get_feedback(rid, conn=conn)
        assert fb["is_example"] == 1
        example_folder = snap / fb["example_dir"]
        assert (example_folder / "street.jpg").exists()

    def test_example_copies_overlay_when_present(self, db_with_snap):
        conn, snap = db_with_snap
        raw = snap / "street_snap.jpg"
        _make_jpeg(raw)
        overlay = snap / "street_snap-lines.jpg"
        # Make overlay a different size so we can distinguish it
        img = Image.new("RGB", (20, 20), (200, 50, 50))
        img.save(str(overlay), "JPEG")

        rid = _seed_reading(conn)
        _seed_lens_reading(conn, rid, label="street", snapshot_path="street_snap.jpg")

        learning.record_feedback(rid, "correct", as_example=True, conn=conn)

        fb = learning.get_feedback(rid, conn=conn)
        example_folder = snap / fb["example_dir"]
        dst = example_folder / "street.jpg"
        assert dst.exists()
        # Verify the overlay (20x20) was copied, not the raw (10x10)
        from PIL import Image as PILImage
        assert PILImage.open(dst).size == (20, 20)

    def test_remove_example_deletes_folder(self, db_with_snap):
        conn, snap = db_with_snap
        raw = snap / "street_snap.jpg"
        _make_jpeg(raw)
        rid = _seed_reading(conn)
        _seed_lens_reading(conn, rid, label="street", snapshot_path="street_snap.jpg")

        learning.record_feedback(rid, "correct", as_example=True, conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        folder = snap / fb["example_dir"]
        assert folder.exists()

        learning.remove_example(rid, conn=conn)

        assert not folder.exists()
        fb2 = learning.get_feedback(rid, conn=conn)
        assert fb2["is_example"] == 0
        assert fb2["example_dir"] is None

    def test_upsert_to_non_example_removes_old_folder(self, db_with_snap):
        conn, snap = db_with_snap
        raw = snap / "street_snap.jpg"
        _make_jpeg(raw)
        rid = _seed_reading(conn)
        _seed_lens_reading(conn, rid, label="street", snapshot_path="street_snap.jpg")

        learning.record_feedback(rid, "correct", as_example=True, conn=conn)
        fb = learning.get_feedback(rid, conn=conn)
        folder = snap / fb["example_dir"]
        assert folder.exists()

        learning.record_feedback(rid, "correct", as_example=False, conn=conn)
        assert not folder.exists()
        fb2 = learning.get_feedback(rid, conn=conn)
        assert fb2["is_example"] == 0


# ---------------------------------------------------------------------------
# select_examples ordering and filtering
# ---------------------------------------------------------------------------

class TestSelectExamples:
    def _make_example(self, conn, snap: Path, status: str, level: float,
                      is_night: int = 0) -> int:
        fname = f"{status}_{int(level)}_n{is_night}.jpg"
        _make_jpeg(snap / fname)
        rid = _seed_reading(conn, status=status, level_index=level)
        _seed_lens_reading(conn, rid, label="cam", snapshot_path=fname, is_night=is_night)
        learning.record_feedback(rid, "correct", true_status=status, true_level_index=level,
                                 as_example=True, conn=conn)
        return rid

    def test_returns_empty_when_k_zero(self, db_with_snap):
        conn, snap = db_with_snap
        result = learning.select_examples(conn=conn, k=0)
        assert result == []

    def test_one_per_status_priority_order(self, db_with_snap):
        conn, snap = db_with_snap
        self._make_example(conn, snap, "normal", 10.0)
        self._make_example(conn, snap, "warning", 55.0)
        self._make_example(conn, snap, "critical", 95.0)

        result = learning.select_examples(conn=conn, k=3)
        statuses = [e["true_status"] for e in result]
        assert set(statuses) == {"normal", "warning", "critical"}
        assert statuses[0] == "critical"

    def test_k_limits_results(self, db_with_snap):
        conn, snap = db_with_snap
        for i in range(5):
            fname = f"snap_{i}.jpg"
            _make_jpeg(snap / fname)
            rid = _seed_reading(conn, status="normal", level_index=float(i * 5))
            _seed_lens_reading(conn, rid, label="cam", snapshot_path=fname)
            learning.record_feedback(rid, "correct", true_status="normal",
                                     true_level_index=float(i * 5), as_example=True, conn=conn)

        result = learning.select_examples(conn=conn, k=2)
        assert len(result) <= 2

    def test_missing_files_skipped(self, db_with_snap):
        conn, snap = db_with_snap
        rid = _seed_reading(conn)
        _seed_lens_reading(conn, rid, label="cam", snapshot_path="ghost.jpg")
        learning.record_feedback(rid, "correct", true_status="normal", as_example=True, conn=conn)
        # Point example_dir to a non-existent folder
        conn.execute(
            "UPDATE feedback SET example_dir = 'examples/9999' WHERE reading_id = ?", (rid,)
        )
        conn.commit()

        result = learning.select_examples(conn=conn, k=5)
        assert all(e["reading_id"] != rid for e in result)

    def test_night_preference(self, db_with_snap):
        conn, snap = db_with_snap
        rid_day = self._make_example(conn, snap, "normal", 10.0, is_night=0)
        rid_night = self._make_example(conn, snap, "normal", 15.0, is_night=1)

        result_day = learning.select_examples(conn=conn, k=1, current_is_night=False)
        assert result_day[0]["reading_id"] == rid_day

        result_night = learning.select_examples(conn=conn, k=1, current_is_night=True)
        assert result_night[0]["reading_id"] == rid_night

    def test_fills_slots_after_one_per_status(self, db_with_snap):
        conn, snap = db_with_snap
        rid1 = self._make_example(conn, snap, "critical", 95.0)
        rid2 = self._make_example(conn, snap, "normal", 10.0)
        rid3 = self._make_example(conn, snap, "normal", 20.0)

        result = learning.select_examples(conn=conn, k=3)
        ids = [e["reading_id"] for e in result]
        assert rid1 in ids
        assert len(result) == 3


# ---------------------------------------------------------------------------
# prune_snapshots leaves examples/ untouched
# ---------------------------------------------------------------------------

class TestPruneSnapshotsLeavesExamples:
    def test_prune_does_not_delete_example_files(self, tmp_path, monkeypatch):
        from wlm.capture import prune_snapshots

        snap = tmp_path / "snapshots"
        snap.mkdir()
        monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap))

        old_root = snap / "old_snap.jpg"
        _make_jpeg(old_root)

        example_dir = snap / "examples" / "42"
        example_dir.mkdir(parents=True)
        example_img = example_dir / "cam.jpg"
        _make_jpeg(example_img)

        # Age files beyond retention
        import time
        old_ts = time.time() - (8 * 24 * 3600)
        os.utime(old_root, (old_ts, old_ts))
        os.utime(example_img, (old_ts, old_ts))

        monkeypatch.setenv("SNAPSHOT_RETENTION_HOURS", "1")

        prune_snapshots()

        assert not old_root.exists(), "Root JPEG should have been pruned"
        assert example_img.exists(), "Example image inside examples/ must survive prune"


# ---------------------------------------------------------------------------
# analyze_images includes example blocks when examples provided
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
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(result_event), stderr=""
    )


def _parse_content_from_mock(mock_run) -> list[dict]:
    """Extract the content blocks from the mocked subprocess.run call."""
    call_kwargs = mock_run.call_args
    stdin = (call_kwargs.kwargs or {}).get("input") or (call_kwargs[1] or {}).get("input", "")
    payload = json.loads(stdin)
    return payload["message"]["content"]


class TestAnalyzeImagesWithExamples:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.frame = self.tmp_dir / "street.jpg"
        _make_jpeg(self.frame)
        self.example_img = self.tmp_dir / "example_cam.jpg"
        _make_jpeg(self.example_img)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _call_with_examples(self, examples):
        from wlm.analysis import analyze_images
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images(
                [("street", self.frame)],
                examples=examples,
            )
        return _parse_content_from_mock(mock_run)

    def test_no_examples_leaves_content_unchanged(self):
        content = self._call_with_examples(None)
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert not any("Verified reference examples" in t for t in texts)
        assert not any("Current images to analyze" in t for t in texts)

    def test_examples_prepended_before_current_images(self):
        examples = [{
            "reading_id": 1,
            "true_status": "critical",
            "true_level_index": 95.0,
            "note": "very flooded",
            "images": [("cam", self.example_img)],
            "is_night": False,
        }]
        content = self._call_with_examples(examples)

        texts = [b["text"] for b in content if b["type"] == "text"]
        image_blocks = [b for b in content if b["type"] == "image"]

        assert "Verified reference examples" in texts[0]

        current_idx = next(i for i, t in enumerate(texts) if "Current images to analyze" in t)
        example_idx = next(i for i, t in enumerate(texts) if "true status: critical" in t)
        assert example_idx < current_idx

        # Two images: one example + one current frame
        assert len(image_blocks) == 2

    def test_prompt_mentions_examples(self):
        examples = [{
            "reading_id": 2,
            "true_status": "normal",
            "true_level_index": 10.0,
            "note": None,
            "images": [("cam", self.example_img)],
            "is_night": False,
        }]
        content = self._call_with_examples(examples)
        texts = [b["text"] for b in content if b["type"] == "text"]
        # The prompt (last text block) references examples
        prompt = texts[-1]
        assert "calibrate" in prompt.lower() or "verified reference examples" in prompt.lower()

    def test_empty_examples_list_no_preamble(self):
        content = self._call_with_examples([])
        texts = [b["text"] for b in content if b["type"] == "text"]
        assert not any("Verified reference examples" in t for t in texts)


# ---------------------------------------------------------------------------
# Web: feedback POST and remove (integration)
# ---------------------------------------------------------------------------

import re as _re


def _reload_web_app():
    if "web.app" in sys.modules:
        del sys.modules["web.app"]
    import web.app
    return web.app.app


@pytest.fixture
def web_app_env(tmp_path, monkeypatch):
    """Minimal web app environment: a seeded DB, snapshot dir, auth configured."""
    db_file = tmp_path / "web_test.db"
    snap = tmp_path / "snaps"
    snap.mkdir()
    monkeypatch.setenv("WLM_DB", str(db_file))
    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass")
    monkeypatch.setenv("DASHBOARD_SECRET", "testsecret1234567890abcdef1234567890abcdef")

    conn = db.connect(db_file)
    # Seed one normal reading with a lens reading
    rid = _seed_reading(conn, status="normal", level_index=20.0)
    _seed_lens_reading(conn, rid, label="street", snapshot_path=None)
    conn.commit()
    conn.close()

    return db_file, snap


async def _authed_web_client(web_app_env):
    from httpx import AsyncClient, ASGITransport
    app = _reload_web_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    # Log in
    resp = await client.get("/login")
    m = _re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    csrf = m.group(1)
    login = await client.post(
        "/login",
        data={"password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    client.cookies.update(login.cookies)
    return client


@pytest.mark.asyncio
async def test_feedback_post_requires_auth(web_app_env):
    from httpx import AsyncClient, ASGITransport
    app = _reload_web_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/reading/1/feedback",
            data={"verdict": "correct", "csrf_token": "bad"},
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302)


@pytest.mark.asyncio
async def test_feedback_post_bad_csrf(web_app_env):
    client = await _authed_web_client(web_app_env)
    try:
        resp = await client.post(
            "/reading/1/feedback",
            data={"verdict": "correct", "csrf_token": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_feedback_post_saves(web_app_env):
    db_file, snap_dir_path = web_app_env
    client = await _authed_web_client(web_app_env)
    try:
        resp = await client.get("/reading/1")
        assert resp.status_code == 200
        m = _re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        assert m, "No CSRF in reading_detail page"
        csrf = m.group(1)

        resp2 = await client.post(
            "/reading/1/feedback",
            data={
                "verdict": "correct",
                "true_status": "normal",
                "true_level_index": "20.0",
                "note": "all good",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 302
    finally:
        await client.aclose()

    conn = db.connect(db_file)
    fb = learning.get_feedback(1, conn=conn)
    conn.close()
    assert fb is not None
    assert fb["verdict"] == "correct"
    assert fb["note"] == "all good"


@pytest.mark.asyncio
async def test_feedback_post_404_on_unknown_reading(web_app_env):
    client = await _authed_web_client(web_app_env)
    try:
        resp = await client.get("/reading/1")
        m = _re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)
        resp2 = await client.post(
            "/reading/99999/feedback",
            data={"verdict": "correct", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp2.status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_feedback_post_400_on_bad_verdict(web_app_env):
    client = await _authed_web_client(web_app_env)
    try:
        resp = await client.get("/reading/1")
        m = _re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)
        resp2 = await client.post(
            "/reading/1/feedback",
            data={"verdict": "bogus", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp2.status_code == 400
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_remove_example_route(web_app_env):
    db_file, snap_dir_path = web_app_env

    conn = db.connect(db_file)
    ex_dir = snap_dir_path / "examples" / "1"
    ex_dir.mkdir(parents=True, exist_ok=True)
    _make_jpeg(ex_dir / "street.jpg")
    conn.execute(
        """INSERT INTO feedback (reading_id, ts, verdict, true_status, is_example, example_dir)
           VALUES (1, '2026-01-01T00:00:00+00:00', 'correct', 'normal', 1, 'examples/1')
           ON CONFLICT(reading_id) DO UPDATE SET is_example=1, example_dir='examples/1'"""
    )
    conn.commit()
    conn.close()

    client = await _authed_web_client(web_app_env)
    try:
        settings_resp = await client.get("/settings")
        m = _re.search(r'name="csrf_token"\s+value="([^"]+)"', settings_resp.text)
        csrf = m.group(1)
        resp = await client.post(
            "/learning/examples/1/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 302
    finally:
        await client.aclose()

    conn2 = db.connect(db_file)
    fb = learning.get_feedback(1, conn=conn2)
    conn2.close()
    assert fb["is_example"] == 0
    assert not ex_dir.exists()


@pytest.mark.asyncio
async def test_settings_page_shows_learning_section(web_app_env):
    client = await _authed_web_client(web_app_env)
    try:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "LEARNING" in resp.text
        assert "TOTAL FEEDBACK RECORDS" in resp.text
    finally:
        await client.aclose()
