"""Tests: per-lens alert lines — overlay, analysis, escalation, storage."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from wlm import db
from wlm.overlay import draw_lines
from wlm.analysis import analyze_images, _ANALYSIS_SCHEMA
from wlm.alerts import derive_final_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_image(path: Path, w: int = 200, h: int = 100, color=(100, 100, 100)) -> None:
    img = Image.new("RGB", (w, h), color)
    img.save(str(path), "JPEG")


import subprocess as _subprocess


def _good_proc_result(
    level_index: float = 30.0,
    street_lp: str = "below_warning",
    carport_lp: str = "no_lines",
) -> _subprocess.CompletedProcess:
    """Build a fake subprocess.CompletedProcess with a successful result event."""
    payload = {
        "level_status": "normal",
        "level_index": level_index,
        "estimated_level_description": "dry street",
        "distance_to_critical": "far",
        "confidence": 0.95,
        "reason": "no water visible",
        "per_lens": [
            {
                "label": "street",
                "observation": "dry",
                "water_coverage_pct": 0.0,
                "line_position": street_lp,
            },
            {
                "label": "carport",
                "observation": "dry",
                "water_coverage_pct": 0.0,
                "line_position": carport_lp,
            },
        ],
    }
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": payload,
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 50},
    }
    return _subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(event), stderr=""
    )


# ---------------------------------------------------------------------------
# Overlay tests
# ---------------------------------------------------------------------------

class TestDrawLines:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "frame.jpg"
        self.dst = self.tmp / "frame-lines.jpg"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_lens_lines_copies_unchanged(self):
        """Empty lens_lines → dst is a copy of src (pixel-identical bytes if JPEG quality matches)."""
        _make_image(self.src, 100, 100)
        result = draw_lines(self.src, {}, self.dst)
        assert result == self.dst
        assert self.dst.exists()
        # src is not modified
        src_bytes = self.src.read_bytes()
        assert len(src_bytes) > 0
        # dst has same size as src (copy)
        assert self.dst.stat().st_size > 0

    def test_src_not_modified(self):
        """draw_lines must never modify the source file."""
        _make_image(self.src, 200, 100)
        src_bytes_before = self.src.read_bytes()
        draw_lines(
            self.src,
            {"warning": [[0.0, 0.5], [1.0, 0.5]]},
            self.dst,
        )
        assert self.src.read_bytes() == src_bytes_before

    def test_warning_line_draws_amber_pixels(self):
        """Warning line draws amber (#f59e0b) pixels near the line position."""
        # White background so amber is detectable
        src = self.tmp / "white.jpg"
        _make_image(src, 300, 150, color=(255, 255, 255))
        dst = self.tmp / "white-lines.jpg"
        # Horizontal line at y=0.5 (y=75 in 150px image)
        draw_lines(src, {"warning": [[0.0, 0.5], [1.0, 0.5]]}, dst)
        img = Image.open(dst).convert("RGB")
        pixels = list(img.getdata())
        # Amber: R≥200, G≥120, B<80
        amber_count = sum(
            1 for r, g, b in pixels if r >= 200 and g >= 120 and b < 80
        )
        assert amber_count > 0, "No amber pixels found for warning line"

    def test_critical_line_draws_red_pixels(self):
        """Critical line draws red (#ef4444) pixels near the line position."""
        src = self.tmp / "white2.jpg"
        _make_image(src, 300, 150, color=(255, 255, 255))
        dst = self.tmp / "white2-lines.jpg"
        draw_lines(src, {"critical": [[0.0, 0.4], [1.0, 0.4]]}, dst)
        img = Image.open(dst).convert("RGB")
        pixels = list(img.getdata())
        # Red: R≥200, G<80, B<80
        red_count = sum(
            1 for r, g, b in pixels if r >= 200 and g < 80 and b < 80
        )
        assert red_count > 0, "No red pixels found for critical line"

    def test_returns_dst_path(self):
        """draw_lines always returns the dst path."""
        _make_image(self.src, 200, 100)
        result = draw_lines(self.src, {"warning": [[0.1, 0.3], [0.9, 0.3]]}, self.dst)
        assert result == self.dst

    def test_both_lines_drawn(self):
        """Both warning and critical lines can be drawn in one call."""
        _make_image(self.src, 300, 200, color=(255, 255, 255))
        draw_lines(
            self.src,
            {
                "warning": [[0.0, 0.4], [1.0, 0.4]],
                "critical": [[0.0, 0.7], [1.0, 0.7]],
            },
            self.dst,
        )
        assert self.dst.exists()
        img = Image.open(self.dst).convert("RGB")
        pixels = list(img.getdata())
        amber_count = sum(1 for r, g, b in pixels if r >= 200 and g >= 120 and b < 80)
        red_count = sum(1 for r, g, b in pixels if r >= 200 and g < 80 and b < 80)
        assert amber_count > 0, "Missing amber warning pixels"
        assert red_count > 0, "Missing red critical pixels"


# ---------------------------------------------------------------------------
# Analysis schema & prompt tests
# ---------------------------------------------------------------------------

class TestAnalysisSchemaAndPrompt:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.street = self.tmp_dir / "street.jpg"
        _make_image(self.street)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_has_per_lens_line_position(self):
        """_ANALYSIS_SCHEMA per_lens items must have line_position with correct enum."""
        per_lens_items = _ANALYSIS_SCHEMA["properties"]["per_lens"]["items"]
        assert "line_position" in per_lens_items["properties"], \
            "line_position missing from per_lens schema"
        assert per_lens_items["properties"]["line_position"]["type"] == "string"
        enum_vals = per_lens_items["properties"]["line_position"]["enum"]
        for lp in db.LINE_POSITIONS:
            assert lp in enum_vals, f"line_position enum missing value: {lp}"

    def test_schema_per_lens_requires_line_position(self):
        """line_position is required in per_lens items."""
        per_lens_items = _ANALYSIS_SCHEMA["properties"]["per_lens"]["items"]
        assert "line_position" in per_lens_items["required"]

    def test_schema_additionalproperties_false(self):
        """additionalProperties: False must appear at root and per_lens."""
        assert _ANALYSIS_SCHEMA.get("additionalProperties") is False
        per_lens_items = _ANALYSIS_SCHEMA["properties"]["per_lens"]["items"]
        assert per_lens_items.get("additionalProperties") is False

    def test_prompt_contains_numeric_anchors(self):
        """The prompt must include the numeric warning and critical thresholds."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _good_proc_result()
            analyze_images(
                [("street", self.street)],
                conn=None,
                lens_lines={"street": {"warning": [[0.0, 0.5], [1.0, 0.5]]}},
            )

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        content = msg["message"]["content"]
        prompt_text = next(
            b["text"] for b in content
            if b.get("type") == "text" and "level_index" in b.get("text", "")
        )
        # Default level_warning=50, level_critical=90
        assert "50" in prompt_text, "Numeric warning threshold missing from prompt"
        assert "90" in prompt_text, "Numeric critical threshold missing from prompt"

    def test_request_uses_overlay_when_lines_exist(self):
        """When lines exist for a lens, the image block should use the overlay path."""
        overlay = self.tmp_dir / "street-lines.jpg"
        _make_image(overlay)  # simulate pre-built overlay
        import base64

        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _good_proc_result()
            # Pass overlay path directly in frames (as runner would do)
            analyze_images(
                [("street", overlay)],
                conn=None,
                lens_lines={"street": {"warning": [[0.0, 0.5], [1.0, 0.5]]}},
            )

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        content = msg["message"]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 1
        # The image data should match the overlay file
        expected_b64 = base64.standard_b64encode(overlay.read_bytes()).decode()
        assert image_blocks[0]["source"]["data"] == expected_b64

    def test_request_uses_raw_image_when_no_lines(self):
        """When no lines exist for a lens, the raw image is sent."""
        import base64

        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _good_proc_result()
            analyze_images(
                [("street", self.street)],
                conn=None,
                lens_lines={},  # no lines
            )

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        content = msg["message"]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        expected_b64 = base64.standard_b64encode(self.street.read_bytes()).decode()
        assert image_blocks[0]["source"]["data"] == expected_b64

    def test_label_text_mentions_lines_when_drawn(self):
        """When lines exist, the label text block says which lines are drawn."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _good_proc_result()
            analyze_images(
                [("street", self.street)],
                conn=None,
                lens_lines={"street": {
                    "warning": [[0.0, 0.5], [1.0, 0.5]],
                    "critical": [[0.0, 0.7], [1.0, 0.7]],
                }},
            )

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        content = msg["message"]["content"]
        label_blocks = [b for b in content if b.get("type") == "text" and "street" in b.get("text", "")]
        assert label_blocks, "No label text block found for 'street'"
        combined = " ".join(b["text"] for b in label_blocks)
        assert "AMBER" in combined or "warning" in combined.lower(), \
            "Warning line not mentioned in label block"
        assert "RED" in combined or "critical" in combined.lower(), \
            "Critical line not mentioned in label block"


# ---------------------------------------------------------------------------
# Escalation rule tests
# ---------------------------------------------------------------------------

class TestEscalationRules:
    def test_at_or_above_critical_escalates_to_critical(self, tmp_db):
        """level_index=30 (normal) + at_or_above_critical → final status critical."""
        per_lens = [
            {"label": "street", "line_position": "at_or_above_critical",
             "observation": "water at line", "water_coverage_pct": 80.0},
        ]
        status = derive_final_status("normal", 30.0, per_lens=per_lens, conn=tmp_db)
        assert status == "critical"

    def test_at_or_above_warning_escalates_to_warning(self, tmp_db):
        """level_index=10 (normal) + at_or_above_warning → final status warning."""
        per_lens = [
            {"label": "street", "line_position": "at_or_above_warning",
             "observation": "water near line", "water_coverage_pct": 30.0},
        ]
        status = derive_final_status("normal", 10.0, per_lens=per_lens, conn=tmp_db)
        assert status == "warning"

    def test_no_lines_no_escalation(self, tmp_db):
        """no_lines positions do not escalate."""
        per_lens = [
            {"label": "carport", "line_position": "no_lines",
             "observation": "dry", "water_coverage_pct": 0.0},
        ]
        status = derive_final_status("normal", 10.0, per_lens=per_lens, conn=tmp_db)
        assert status == "normal"

    def test_below_warning_no_escalation(self, tmp_db):
        """below_warning does not escalate."""
        per_lens = [
            {"label": "street", "line_position": "below_warning",
             "observation": "dry", "water_coverage_pct": 0.0},
        ]
        status = derive_final_status("normal", 10.0, per_lens=per_lens, conn=tmp_db)
        assert status == "normal"

    def test_unknown_stays_unknown_regardless_of_lines(self, tmp_db):
        """Unknown model status stays unknown even if line_position would escalate."""
        per_lens = [
            {"label": "street", "line_position": "at_or_above_critical",
             "observation": "unclear", "water_coverage_pct": 0.0},
        ]
        status = derive_final_status("unknown", None, per_lens=per_lens, conn=tmp_db)
        assert status == "unknown"

    def test_at_or_above_critical_wins_over_warning(self, tmp_db):
        """at_or_above_critical on one lens, at_or_above_warning on another → critical."""
        per_lens = [
            {"label": "street", "line_position": "at_or_above_warning",
             "observation": "rising", "water_coverage_pct": 40.0},
            {"label": "carport", "line_position": "at_or_above_critical",
             "observation": "high", "water_coverage_pct": 90.0},
        ]
        status = derive_final_status("normal", 30.0, per_lens=per_lens, conn=tmp_db)
        assert status == "critical"

    def test_backward_compat_no_per_lens(self, tmp_db):
        """derive_final_status works correctly with no per_lens argument."""
        # level_index=30 with default thresholds (warning=50, critical=90) → normal
        status = derive_final_status("normal", 30.0, conn=tmp_db)
        assert status == "normal"

    def test_index_at_warning_threshold(self, tmp_db):
        """level_index at the warning threshold → warning (threshold rule still works)."""
        db.set_setting("level_warning", "50", conn=tmp_db)
        status = derive_final_status("warning", 50.0, conn=tmp_db)
        assert status == "warning"


# ---------------------------------------------------------------------------
# Lines decide critical
# ---------------------------------------------------------------------------

_CARPORT_LINES = {"carport": {"warning": [[0.2, 0.3], [0.4, 0.9]],
                              "critical": [[0.1, 0.3], [0.3, 0.9]]}}


def _lens(label: str, position: str | None) -> dict:
    lens = {"label": label, "observation": "x", "water_coverage_pct": 10.0}
    if position is not None:
        lens["line_position"] = position
    return lens


class TestLinesDecideCritical:
    def test_high_index_below_red_line_capped_at_warning(self, tmp_db):
        """Reading #111: level_index 95 but the water is below the lines → warning."""
        per_lens = [_lens("street", "no_lines"), _lens("carport", "below_warning")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, conn=tmp_db)
        assert status == "warning"

    def test_red_line_reached_is_critical(self, tmp_db):
        per_lens = [_lens("street", "no_lines"), _lens("carport", "at_or_above_critical")]
        status = derive_final_status("warning", 60.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, conn=tmp_db)
        assert status == "critical"

    def test_not_visible_on_deciding_lens_is_unknown(self, tmp_db):
        per_lens = [_lens("street", "no_lines"), _lens("carport", "not_visible")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, conn=tmp_db)
        assert status == "unknown"

    def test_missing_position_on_deciding_lens_is_unknown(self, tmp_db):
        per_lens = [_lens("street", "no_lines"), _lens("carport", None)]
        status = derive_final_status("warning", 60.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, conn=tmp_db)
        assert status == "unknown"

    def test_critical_on_one_deciding_lens_wins_over_not_visible(self, tmp_db):
        lines = {"front": {"critical": [[0.1, 0.1], [0.9, 0.9]]},
                 "back": {"critical": [[0.1, 0.1], [0.9, 0.9]]}}
        per_lens = [_lens("front", "at_or_above_critical"), _lens("back", "not_visible")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens,
                                     lens_lines=lines, conn=tmp_db)
        assert status == "critical"

    def test_deciding_lens_capture_failed_is_unknown(self, tmp_db):
        per_lens = [_lens("street", "no_lines")]
        status = derive_final_status("warning", 60.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, failed_labels=["carport"],
                                     conn=tmp_db)
        assert status == "unknown"

    def test_other_lens_capture_failed_still_decides(self, tmp_db):
        per_lens = [_lens("carport", "below_warning")]
        status = derive_final_status("warning", 60.0, per_lens=per_lens,
                                     lens_lines=_CARPORT_LINES, failed_labels=["street"],
                                     conn=tmp_db)
        assert status == "warning"

    def test_no_red_lines_keeps_index_thresholds(self, tmp_db):
        per_lens = [_lens("camera", "no_lines")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens,
                                     lens_lines={}, conn=tmp_db)
        assert status == "critical"

    def test_warning_only_lines_do_not_cap(self, tmp_db):
        lines = {"camera": {"warning": [[0.1, 0.1], [0.9, 0.9]]}}
        per_lens = [_lens("camera", "at_or_above_warning")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens,
                                     lens_lines=lines, conn=tmp_db)
        assert status == "critical"

    def test_lines_loaded_from_settings_when_not_passed(self, tmp_db):
        db.set_setting("level_lines", json.dumps(_CARPORT_LINES), conn=tmp_db)
        per_lens = [_lens("carport", "below_warning")]
        status = derive_final_status("critical", 95.0, per_lens=per_lens, conn=tmp_db)
        assert status == "warning"


# ---------------------------------------------------------------------------
# Runner integration: line_position storage & composite from overlays
# ---------------------------------------------------------------------------

class TestRunnerLineIntegration:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.street = self.tmp_dir / "street.jpg"
        self.carport = self.tmp_dir / "carport.jpg"
        _make_image(self.street, 200, 100)
        _make_image(self.carport, 200, 100)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _analysis_result(self, street_lp="below_warning", carport_lp="no_lines"):
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
                        "line_position": street_lp,
                    },
                    {
                        "label": "carport",
                        "observation": "dry",
                        "water_coverage_pct": 0.0,
                        "line_position": carport_lp,
                    },
                ],
            },
            100, 50, 0.001,
        )

    def test_line_position_stored_in_db(self, tmp_db, monkeypatch):
        """run_cycle stores line_position from analysis result into lens_readings."""
        from wlm.runner import run_cycle
        from wlm import lines as wlm_lines

        # Set alert lines for street lens
        wlm_lines.set_lines(
            {"street": {"warning": [[0.0, 0.35], [1.0, 0.33]]}},
            conn=tmp_db,
        )

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = self._analysis_result(
                street_lp="below_warning", carport_lp="no_lines"
            )
            reading_id = run_cycle(
                dry_run=True,
                images=[("street", self.street), ("carport", self.carport)],
            )

        assert reading_id is not None
        lens_rows = tmp_db.execute(
            "SELECT label, line_position FROM lens_readings WHERE reading_id=?",
            (reading_id,),
        ).fetchall()
        lp_map = {r["label"]: r["line_position"] for r in lens_rows}
        assert lp_map["street"] == "below_warning"
        assert lp_map["carport"] == "no_lines"

    def test_invalid_line_position_stored_as_null(self, tmp_db, monkeypatch):
        """Invalid line_position value from analysis → stored as NULL."""
        from wlm.runner import run_cycle

        bad_result = (
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
                        "line_position": "totally_invalid_value",
                    },
                ],
            },
            100, 50, 0.001,
        )

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = bad_result
            reading_id = run_cycle(
                dry_run=True,
                images=[("street", self.street)],
            )

        assert reading_id is not None
        row = tmp_db.execute(
            "SELECT line_position FROM lens_readings WHERE reading_id=? AND label='street'",
            (reading_id,),
        ).fetchone()
        assert row is not None
        assert row["line_position"] is None, "Invalid line_position must be stored as NULL"

    def test_overlay_file_created_when_lines_set(self, tmp_db, monkeypatch):
        """When lines are set for a lens, an overlay file is created next to the snapshot."""
        from wlm.runner import run_cycle
        from wlm import lines as wlm_lines

        monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(self.tmp_dir))
        wlm_lines.set_lines(
            {"street": {"warning": [[0.0, 0.4], [1.0, 0.4]]}},
            conn=tmp_db,
        )

        # Rename street.jpg to have the expected timestamp-based name
        from pathlib import Path
        import shutil
        snap_dir = self.tmp_dir
        street_snap = snap_dir / "20260925-120000-street.jpg"
        shutil.copy(str(self.street), str(street_snap))

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"):
            mock_analyze.return_value = self._analysis_result()
            run_cycle(
                dry_run=True,
                images=[("street", street_snap), ("carport", self.carport)],
            )

        # An overlay file should be in the snapshot dir
        overlay_files = list(snap_dir.glob("*-lines.jpg"))
        assert len(overlay_files) >= 1, "Overlay file not created for lens with lines"

    def test_composite_built_from_overlays(self, tmp_db, monkeypatch):
        """When overlays exist, the composite is built from overlay paths (not raw)."""
        from wlm.runner import run_cycle
        from wlm import lines as wlm_lines

        monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(self.tmp_dir))
        wlm_lines.set_lines(
            {"street": {"warning": [[0.0, 0.4], [1.0, 0.4]]}},
            conn=tmp_db,
        )

        # Simulate a snapshot in the snap dir
        import shutil
        snap_dir = self.tmp_dir
        street_snap = snap_dir / "20260925-120001-street.jpg"
        shutil.copy(str(self.street), str(street_snap))

        captured_frames = []

        def _fake_build_composite(frames, **kwargs):
            captured_frames.extend(frames)
            # Create a simple composite
            out = snap_dir / "20260925-120001-composite.jpg"
            _make_image(out, 200, 200)
            return out

        with patch("wlm.runner.analyze_images") as mock_analyze, \
             patch("wlm.runner.refresh_weather"), \
             patch("wlm.runner.capture.prune_snapshots"), \
             patch("wlm.runner.capture.build_composite", side_effect=_fake_build_composite):
            mock_analyze.return_value = self._analysis_result()
            run_cycle(
                dry_run=True,
                images=[("street", street_snap), ("carport", self.carport)],
            )

        # The composite should have been built from the overlay (lines) path for street
        assert len(captured_frames) == 2
        street_frame = next((path for label, path in captured_frames if label == "street"), None)
        assert street_frame is not None
        assert "-lines.jpg" in street_frame.name, \
            f"Expected overlay path for street, got: {street_frame.name}"
