"""Tests: analysis request shape, error handling — via mocked subprocess.run."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from wlm.analysis import analyze_images, _unknown_result


def _make_image(path: Path, w: int = 100, h: int = 100) -> None:
    img = Image.new("RGB", (w, h), (100, 100, 100))
    img.save(str(path), "JPEG")


def _good_structured(level_index: float = 30.0) -> dict:
    return {
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
                "line_position": "no_lines",
            },
        ],
    }


def _make_proc_result(
    structured_output=None,
    usage=None,
    cost: float = 0.001,
    subtype: str = "success",
    is_error: bool = False,
    result_text: str = "",
    returncode: int = 0,
    stderr: str = "",
    extra_events: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Build a fake subprocess.CompletedProcess with a result event in stdout."""
    if usage is None:
        usage = {
            "input_tokens": 100,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
            "output_tokens": 50,
        }
    result_event = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result_text,
        "structured_output": structured_output,
        "total_cost_usd": cost,
        "usage": usage,
    }
    lines = []
    if extra_events:
        lines.extend(extra_events)
    lines.append(json.dumps(result_event))
    stdout = "\n".join(lines)
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestAnalysisRequestShape:
    """Verify the CLI is called with correct argv and stdin structure."""

    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.street = self.tmp_dir / "street.jpg"
        self.carport = self.tmp_dir / "carport.jpg"
        _make_image(self.street)
        _make_image(self.carport)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_argv_contains_json_schema(self):
        """--json-schema flag must be present and its value must be valid JSON with the schema."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.street)], conn=None)

        argv = mock_run.call_args[0][0]
        assert "--json-schema" in argv
        schema_val = argv[argv.index("--json-schema") + 1]
        schema = json.loads(schema_val)
        assert "per_lens" in schema["properties"]
        assert "level_index" in schema["properties"]

    def test_argv_contains_input_format_stream_json(self):
        """--input-format stream-json must be in argv."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.street)], conn=None)

        argv = mock_run.call_args[0][0]
        assert "--input-format" in argv
        assert argv[argv.index("--input-format") + 1] == "stream-json"

    def test_argv_contains_tools_empty_string(self):
        """--tools '' (empty string) must be in argv to disable tool use."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.street)], conn=None)

        argv = mock_run.call_args[0][0]
        assert "--tools" in argv
        assert argv[argv.index("--tools") + 1] == ""

    def test_stdin_contains_image_blocks_in_label_order(self):
        """stdin must be a JSON line with image content blocks for each lens in order."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.street), ("carport", self.carport)], conn=None)

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        content = msg["message"]["content"]

        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 2

        text_blocks = [b for b in content if b.get("type") == "text"]
        label_texts = [b["text"] for b in text_blocks]
        assert any("street" in t for t in label_texts)
        assert any("carport" in t for t in label_texts)

        # Each label text block must appear before its image block
        street_text_idx = next(i for i, b in enumerate(content)
                               if b.get("type") == "text" and "street" in b.get("text", ""))
        street_image_idx = next(i for i, b in enumerate(content) if b.get("type") == "image")
        assert street_text_idx < street_image_idx

    def test_stdin_image_data_is_valid_base64(self):
        """Image blocks in stdin must carry valid base64-encoded JPEG data."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.street)], conn=None)

        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        image_block = next(b for b in msg["message"]["content"] if b.get("type") == "image")
        raw = base64.b64decode(image_block["source"]["data"])
        # JPEG magic bytes
        assert raw[:2] == b"\xff\xd8"

    def test_tokens_and_cost_mapped_from_usage(self):
        """input_tokens sums input + cache_creation + cache_read; cost from total_cost_usd."""
        usage = {
            "input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 40,
        }
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured(), usage=usage, cost=0.0042)
            _, in_tok, out_tok, cost = analyze_images([("street", self.street)], conn=None)

        assert in_tok == 150   # 100 + 20 + 30
        assert out_tok == 40
        assert abs(cost - 0.0042) < 1e-9

    def test_level_index_clamped_above_100(self):
        """level_index > 100 must be clamped to 100.0."""
        structured = _good_structured(level_index=150.0)
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(structured)
            result, _, _, _ = analyze_images([("street", self.street)], conn=None)

        assert result["level_index"] == 100.0

    def test_level_index_clamped_below_0(self):
        """level_index < 0 must be clamped to 0.0."""
        structured = _good_structured(level_index=-10.0)
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(structured)
            result, _, _, _ = analyze_images([("street", self.street)], conn=None)

        assert result["level_index"] == 0.0


class TestAnalysisErrors:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp()) / "img.jpg"
        _make_image(self.tmp)

    def teardown_method(self):
        if self.tmp.exists():
            self.tmp.unlink()

    def test_empty_frames_returns_unknown(self):
        result, i, o, c = analyze_images([], conn=None)
        assert result["level_status"] == "unknown"
        assert i == 0 and o == 0

    def test_missing_binary_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value=None):
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "not found" in result["reason"].lower()

    def test_timeout_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=180)):
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "timed out" in result["reason"].lower()

    def test_nonzero_exit_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="some error"
            )
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "1" in result["reason"]

    def test_nonzero_exit_reports_result_text_from_stdout(self):
        stdout = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                             "result": "Not logged in · Please run /login"}) + "\n"
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=stdout, stderr=""
            )
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "Not logged in" in result["reason"]

    def test_is_error_result_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(
                structured_output=None,
                subtype="error",
                is_error=True,
                result_text="something went wrong",
            )
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "error" in result["reason"].lower()

    def test_no_result_event_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout='{"type":"assistant_message","content":"hi"}\n',
                stderr="",
            )
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "result event" in result["reason"].lower() or "no result" in result["reason"].lower()

    def test_missing_structured_output_returns_unknown(self):
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(
                structured_output=None,
                subtype="success",
                is_error=False,
                result_text="some unstructured text",
            )
            result, _, _, _ = analyze_images([("cam", self.tmp)], conn=None)

        assert result["level_status"] == "unknown"
        assert "structured_output" in result["reason"].lower()


class TestAnalysisPromptContent:
    """Verify the prompt contains required guidance."""

    def setup_method(self):
        import tempfile
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.img = self.tmp_dir / "street.jpg"
        _make_image(self.img)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _capture_prompt(self):
        """Return the prompt text block from the stdin payload."""
        with patch("wlm.analysis.shutil.which", return_value="/usr/bin/claude"), \
             patch("wlm.analysis.subprocess.run") as mock_run:
            mock_run.return_value = _make_proc_result(_good_structured())
            analyze_images([("street", self.img)], conn=None)
        stdin_str = mock_run.call_args.kwargs["input"]
        msg = json.loads(stdin_str)
        # The last content block is the prompt text
        text_blocks = [b["text"] for b in msg["message"]["content"] if b.get("type") == "text"]
        return "\n".join(text_blocks)

    def test_prompt_contains_low_light_guidance(self):
        """Prompt must mention reflections and the 'not_visible' fallback."""
        prompt = self._capture_prompt()
        assert "not standing water" in prompt.lower() or "dark, shiny" in prompt.lower()
        assert "not_visible" in prompt

    def test_prompt_mentions_all_line_positions(self):
        """All LINE_POSITIONS values must appear in the prompt."""
        from wlm.db import LINE_POSITIONS
        prompt = self._capture_prompt()
        for pos in LINE_POSITIONS:
            assert pos in prompt, f"LINE_POSITIONS value '{pos}' missing from prompt"
