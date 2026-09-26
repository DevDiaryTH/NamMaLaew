"""Claude vision analysis for water-level monitoring via Claude Code CLI."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from wlm import db, settings

logger = logging.getLogger("wlm.analysis")

# Legacy pricing table kept for backward compatibility (test_core_status uses it).
# With a Claude subscription the CLI reports total_cost_usd which is what the
# dashboard stores; this table is no longer used for new analysis calls.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "level_status": {
            "type": "string",
            "enum": ["normal", "warning", "critical", "unknown"],
        },
        "level_index": {"type": "number"},
        "estimated_level_description": {"type": "string"},
        "distance_to_critical": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "per_lens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "observation": {"type": "string"},
                    "water_coverage_pct": {"type": "number"},
                    "line_position": {
                        "type": "string",
                        "enum": list(db.LINE_POSITIONS),
                    },
                },
                "required": ["label", "observation", "water_coverage_pct", "line_position"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "level_status",
        "level_index",
        "estimated_level_description",
        "distance_to_critical",
        "confidence",
        "reason",
        "per_lens",
    ],
    "additionalProperties": False,
}


def _unknown_result(reason: str) -> dict:
    return {
        "level_status": "unknown",
        "level_index": None,
        "estimated_level_description": "Unable to determine",
        "distance_to_critical": "Unknown",
        "confidence": 0.0,
        "reason": reason,
        "per_lens": [],
    }


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Legacy cost estimate from token counts. Kept for backward compatibility."""
    pricing = PRICING.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def analyze_images(
    frames: list[tuple[str, Path]],
    conn=None,
    lens_lines: dict | None = None,
) -> tuple[dict, int, int, float | None]:
    """Analyze all lens images in one Claude Code CLI call.

    Args:
        frames: list of (label, path) — when overlay images exist for a lens,
            pass the overlay path here instead of the raw frame.
        conn: optional DB connection for settings lookup.
        lens_lines: dict mapping lens label → {"warning": [[x,y],...], "critical": [[x,y],...]}
            for lenses that have user-drawn alert lines.  Used to build per-lens
            descriptions in the prompt.  When None, falls back to the old
            no-lines behaviour.

    Returns (result_dict, input_tokens, output_tokens, cost_usd).
    result_dict always has level_status, level_index (clamped 0-100 or None),
    estimated_level_description, distance_to_critical, confidence, reason, per_lens.
    per_lens items include line_position (see db.LINE_POSITIONS).
    """
    if not frames:
        return _unknown_result("No frames available for analysis"), 0, 0, None

    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        logger.error("claude CLI not found at %r (set CLAUDE_BIN to override)", claude_bin)
        return _unknown_result("claude CLI not found"), 0, 0, None

    timeout = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "180"))
    model = settings.get("claude_model", conn=conn)
    reference_description = settings.get("reference_description", conn=conn)
    level_warning = settings.get_float("level_warning", conn=conn)
    level_critical = settings.get_float("level_critical", conn=conn)

    if lens_lines is None:
        lens_lines = {}

    # Build content blocks: label text block (with line description) before each image
    content: list[dict] = []
    for label, path in frames:
        lines_for_lens = lens_lines.get(label, {})
        if lines_for_lens:
            parts = []
            if "warning" in lines_for_lens:
                parts.append("a dashed AMBER line = warning level")
            if "critical" in lines_for_lens:
                parts.append("a dashed RED line = critical level")
            line_desc = f"  Lines drawn on this image: {', '.join(parts)}."
        else:
            line_desc = ""
        label_text = f"Lens '{label}':{line_desc}"
        image_b64 = base64.standard_b64encode(path.read_bytes()).decode()
        content.append({"type": "text", "text": label_text})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        })

    # Build lines section of the prompt
    lines_section = (
        "Alert lines (when drawn on an image):\n"
        f"  - Dashed AMBER line = warning level (level_index ≈ {level_warning:.0f})\n"
        f"  - Dashed RED line   = critical level (level_index ≈ {level_critical:.0f})\n"
        "For each lens that has lines drawn on it, decide where the actual waterline / "
        "standing-water edge is relative to each line.  Use the line_position field:\n"
        "  'no_lines'              — no alert lines were drawn on this lens image\n"
        "  'not_visible'           — lines are drawn but the water edge cannot be judged\n"
        "  'below_warning'         — water is below the warning line (or no water visible)\n"
        "  'at_or_above_warning'   — water has reached or exceeded the warning line\n"
        "  'at_or_above_critical'  — water has reached or exceeded the critical line\n"
        "When a lens image has no lines, always set line_position to 'no_lines'.\n"
    )

    prompt = (
        "You are a water-level safety monitor analyzing security camera images.\n"
        "Note: cameras may produce night-vision IR grayscale images — this is normal.\n"
        "If you cannot see water or cannot determine the level, use level_status='unknown' (level_index is then ignored; set it to 0).\n\n"
        f"Reference description (defines the level_index 0-100 scale):\n{reference_description}\n\n"
        "level_index scale: 0=completely dry (no water anywhere), 100=maximum flooding (water covers carport floor / car wheels).\n\n"
        "For each lens, estimate water_coverage_pct = percentage of the visible ground area in that lens covered by standing water (0-100).\n\n"
        + lines_section
        + "\nAnalyze all provided images together and return a JSON object matching the schema exactly."
    )
    content.append({"type": "text", "text": prompt})

    # Build the stream-json stdin payload (one line = one user message)
    stdin_payload = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
    })

    system_prompt = (
        "You are a water-level safety monitor. Output only JSON matching the provided schema."
    )

    argv = [
        claude_bin, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(_ANALYSIS_SCHEMA),
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
    ]

    try:
        proc = subprocess.run(
            argv,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("claude CLI timed out after %ds", timeout)
        return _unknown_result("claude CLI timed out"), 0, 0, None
    except FileNotFoundError:
        logger.error("claude CLI binary not found: %r", claude_bin)
        return _unknown_result("claude CLI not found"), 0, 0, None

    # Parse newline-delimited JSON events from stdout; find the result event
    result_event: dict | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result_event = event
            break

    if proc.returncode != 0:
        # The CLI reports failures such as "Not logged in" in the stream-json result
        # event on stdout, leaving stderr empty.
        detail = str((result_event or {}).get("result") or proc.stderr or "")[:200]
        logger.error("claude CLI exited %d: %s", proc.returncode, detail)
        return _unknown_result(f"claude CLI exited {proc.returncode}: {detail}"), 0, 0, None

    if result_event is None:
        stderr_snippet = (proc.stderr or "")[:200]
        logger.error("No result event in claude CLI output; stderr: %s", stderr_snippet)
        return _unknown_result(f"No result event from claude CLI: {stderr_snippet}"), 0, 0, None

    if result_event.get("is_error") or result_event.get("subtype") != "success":
        result_text = str(result_event.get("result", ""))[:200]
        logger.error("claude CLI returned error result: %s", result_text)
        return _unknown_result(f"claude CLI error: {result_text}"), 0, 0, None

    structured = result_event.get("structured_output")
    if structured is None:
        result_text = str(result_event.get("result", ""))[:200]
        logger.error("No structured_output in result event: %s", result_text)
        return _unknown_result(f"No structured_output in result: {result_text}"), 0, 0, None

    usage = result_event.get("usage") or {}
    input_tokens = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )
    output_tokens = usage.get("output_tokens") or 0
    cost_usd = result_event.get("total_cost_usd")

    # Clamp level_index to 0-100
    if structured.get("level_index") is not None:
        structured["level_index"] = max(0.0, min(100.0, float(structured["level_index"])))

    logger.info(
        "Analysis: status=%s level_index=%s confidence=%.2f",
        structured.get("level_status"),
        structured.get("level_index"),
        structured.get("confidence", 0.0),
    )
    return structured, input_tokens, output_tokens, cost_usd
