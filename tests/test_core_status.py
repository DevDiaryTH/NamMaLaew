"""Tests: status derivation, level_index clamp, cost calculation."""

from __future__ import annotations

import pytest

from wlm.alerts import derive_final_status
from wlm.analysis import _compute_cost, PRICING


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------

class TestDeriveStatus:
    """derive_final_status maps model output + level_index → final status."""

    def test_normal_below_warning(self, tmp_db):
        # level_index 0 is well below default warning (50)
        assert derive_final_status("normal", 0.0, conn=tmp_db) == "normal"

    def test_normal_just_below_warning(self, tmp_db):
        assert derive_final_status("normal", 49.9, conn=tmp_db) == "normal"

    def test_warning_at_threshold(self, tmp_db):
        # default level_warning = 50
        assert derive_final_status("warning", 50.0, conn=tmp_db) == "warning"

    def test_warning_above_threshold(self, tmp_db):
        assert derive_final_status("normal", 70.0, conn=tmp_db) == "warning"

    def test_critical_at_threshold(self, tmp_db):
        # default level_critical = 90
        assert derive_final_status("critical", 90.0, conn=tmp_db) == "critical"

    def test_critical_above_threshold(self, tmp_db):
        assert derive_final_status("warning", 95.0, conn=tmp_db) == "critical"

    def test_unknown_when_model_says_unknown(self, tmp_db):
        assert derive_final_status("unknown", 50.0, conn=tmp_db) == "unknown"

    def test_unknown_when_level_index_none(self, tmp_db):
        assert derive_final_status("warning", None, conn=tmp_db) == "unknown"

    def test_unknown_when_model_status_none(self, tmp_db):
        assert derive_final_status(None, 60.0, conn=tmp_db) == "unknown"

    def test_critical_overrides_model_normal(self, tmp_db):
        # Even if model says "normal", high level_index → critical
        assert derive_final_status("normal", 91.0, conn=tmp_db) == "critical"


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

class TestCostCalc:
    def test_known_model(self):
        # claude-opus-5: input $5/1M, output $25/1M
        cost = _compute_cost("claude-opus-5", 1_000_000, 1_000_000)
        assert abs(cost - 30.0) < 1e-6

    def test_haiku_cheap(self):
        # claude-haiku-4-5: input $1/1M, output $5/1M
        cost = _compute_cost("claude-haiku-4-5", 1_000, 500)
        expected = (1_000 * 1.0 + 500 * 5.0) / 1_000_000
        assert abs(cost - expected) < 1e-9

    def test_unknown_model_returns_none(self):
        assert _compute_cost("gpt-9000", 100, 100) is None

    def test_all_known_models_have_pricing(self):
        for model in ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"]:
            cost = _compute_cost(model, 1000, 1000)
            assert cost is not None and cost > 0

    def test_zero_tokens(self):
        assert _compute_cost("claude-opus-5", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# level_index clamping (via analysis module)
# ---------------------------------------------------------------------------

class TestLevelIndexClamp:
    def _make_proc(self, level_index: float) -> "subprocess.CompletedProcess":
        import json
        import subprocess
        structured = {
            "level_status": "critical" if level_index > 100 else "normal",
            "level_index": level_index,
            "estimated_level_description": "test",
            "distance_to_critical": "none",
            "confidence": 0.9,
            "reason": "test",
            "per_lens": [],
        }
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "structured_output": structured,
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "output_tokens": 10},
        }
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(event), stderr=""
        )

    def test_clamp_above_100(self):
        from wlm.analysis import analyze_images
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        from PIL import Image

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            Image.new("RGB", (100, 100), (128, 128, 128)).save(f.name)
            tmp = Path(f.name)
        try:
            with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
                 patch("wlm.analysis.subprocess.run", return_value=self._make_proc(150.0)):
                result, _, _, _ = analyze_images([("test", tmp)], conn=None)
        finally:
            tmp.unlink(missing_ok=True)

        assert result["level_index"] == 100.0

    def test_clamp_below_0(self):
        from wlm.analysis import analyze_images
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        from PIL import Image

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            Image.new("RGB", (100, 100)).save(f.name)
            tmp = Path(f.name)
        try:
            with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
                 patch("wlm.analysis.subprocess.run", return_value=self._make_proc(-10.0)):
                result, _, _, _ = analyze_images([("test", tmp)], conn=None)
        finally:
            tmp.unlink(missing_ok=True)

        assert result["level_index"] == 0.0
