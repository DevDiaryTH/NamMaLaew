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

# Hard server-side caps for free-text fields returned by the model.
# These are intentionally looser than the prompt targets so they only fire on
# extreme overruns, not on well-formed but slightly long responses.
_CAP_DESC = 120       # estimated_level_description
_CAP_REASON = 240     # reason
_CAP_DISTANCE = 60    # distance_to_critical
_CAP_OBS = 180        # per_lens[].observation


def _cap_text(text: object, limit: int) -> object:
    """Truncate *text* to at most *limit* chars, cutting at the last word boundary.

    Appends "…" when truncation occurs.  Non-string values are returned unchanged
    so that None and numeric sentinels pass through unmodified.
    """
    if not isinstance(text, str) or len(text) <= limit:
        return text
    # Cut at the last space at or before (limit - 1) to leave room for "…"
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit - 1
    return text[:cut] + "…"


def normalize_confidence(value: object) -> float | None:
    """Return confidence on the 0-1 scale.

    The model sometimes answers on a 0-100 scale (e.g. 78 instead of 0.78); values
    above 1 are treated as percentages. The result is clamped to 0-1.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


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
    examples: list[dict] | None = None,
    history_text: str | None = None,
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
        examples: optional list of verified reference examples from learning.select_examples().
            When non-empty, prepended as few-shot calibration images before the current frames.
        history_text: optional summary of recent readings produced by learning.recent_context().
            When given, inserted between the examples and the current images so Claude has
            recent context; behaviour is unchanged when None.

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

    # Build content blocks
    content: list[dict] = []

    # Prepend verified reference examples when available
    if examples:
        content.append({
            "type": "text",
            "text": (
                "Verified reference examples from THIS site (human-confirmed ground truth). "
                "Use them to calibrate the level_index scale and what water looks like here; "
                "they are NOT the current scene — judge the current images on their own evidence."
            ),
        })
        for i, ex in enumerate(examples, 1):
            note_part = f", note: {ex['note']}" if ex.get("note") else ""
            content.append({
                "type": "text",
                "text": (
                    f"Example {i} — true status: {ex['true_status']}, "
                    f"true level_index: {ex['true_level_index']}{note_part}"
                ),
            })
            for label, path in ex.get("images", []):
                image_b64 = base64.standard_b64encode(path.read_bytes()).decode()
                content.append({"type": "text", "text": f"Example {i} lens '{label}':"})
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    },
                })

    # Recent-history context block (between examples and current images)
    if history_text:
        content.append({
            "type": "text",
            "text": "Recent readings at this site (context only):\n" + history_text,
        })

    # Separator appears whenever the model has seen examples or history
    if examples or history_text:
        content.append({"type": "text", "text": "Current images to analyze:"})

    # Current-lens blocks: label text block (with line description) before each image
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
        "When lines are drawn on a lens, line_position is the deciding evidence.  Report\n"
        "  'at_or_above_critical' only when the standing-water edge visibly touches or crosses\n"
        "  the RED line itself; water elsewhere in the frame does not count.  Do not set\n"
        f"  level_index at or above {level_critical:.0f} unless the water has reached the red line.\n"
    )

    low_light_section = (
        "Low light and reflections:\n"
        "  A dark, shiny or wet-looking floor is NOT standing water unless there is a visible\n"
        "  water edge/waterline, ripples, or objects reflected in a continuous water surface.\n"
        "  Tiles, painted concrete and car reflections at dawn/dusk or under IR illumination\n"
        "  often appear wet or flooded; do not classify them as standing water on appearance\n"
        "  alone.  When the water edge relative to a drawn line cannot be judged with\n"
        "  confidence, use line_position 'not_visible' rather than guessing.\n"
        "  Judge each lens from its own image; do not infer one lens's water level from another lens.\n"
    )

    output_style_section = (
        "Output style (short and scannable — lead with the conclusion):\n"
        "  estimated_level_description: 1 sentence, ≤ ~80 chars, where the water is.\n"
        '    Example: "Water up to the gate; deciding area dry."\n'
        "  reason: ONE sentence, ≤ ~120 chars: the deciding evidence → the status. Count the characters;\n"
        "    stop before 120 even if detail is lost.  Do not restate the level_index or coverage numbers.\n"
        '    Example: "Water below amber line on deciding lens; flooded up to gate → WARNING."\n'
        "  distance_to_critical: ≤ ~40 chars.\n"
        '    Example: "~30 cm below red line"\n'
        "  per_lens observation: 1 sentence, ≤ ~120 chars, water facts only.\n"
        "  Lead with the conclusion. No filler, no hedging, no repeated restating of the schema or scale.\n"
        "  Do not describe things unrelated to water (car colors, furniture, background scenery).\n"
    )

    examples_sentence = (
        " Use the verified reference examples above to calibrate your level_index scale for this site."
        if examples else ""
    )
    history_paragraph = (
        "\nRecent history: water levels usually change gradually between readings. "
        "Judge the current images FIRST on their own visual evidence; use the recent readings "
        "above only as a soft prior. Do not copy previous values: if the images clearly show a "
        "change, report it even if it is large; if the evidence is ambiguous, a result close to "
        "the recent trend is more likely than a sudden jump.\n"
        if history_text else ""
    )
    prompt = (
        "You are a water-level safety monitor analyzing security camera images.\n"
        "Note: cameras may produce night-vision IR grayscale images — this is normal.\n"
        "If you cannot see water or cannot determine the level, use level_status='unknown' (level_index is then ignored; set it to 0).\n\n"
        f"Reference description (defines the level_index 0-100 scale):\n{reference_description}\n\n"
        "level_index scale: 0=completely dry (no water anywhere), 100=worst flooding as defined in the reference description above.\n"
        "confidence is a fraction from 0.0 to 1.0 (e.g. 0.78), not a percentage.\n\n"
        "For each lens, estimate water_coverage_pct = percentage of the visible ground area in that lens covered by standing water (0-100).\n\n"
        + lines_section
        + low_light_section
        + output_style_section
        + history_paragraph
        + f"\nAnalyze all provided images together and return a JSON object matching the schema exactly.{examples_sentence}"
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

    # Apply hard server-side caps to free-text fields (model output only; error
    # messages produced by _unknown_result are intentionally not touched here).
    structured["estimated_level_description"] = _cap_text(
        structured.get("estimated_level_description"), _CAP_DESC
    )
    structured["reason"] = _cap_text(structured.get("reason"), _CAP_REASON)
    structured["distance_to_critical"] = _cap_text(
        structured.get("distance_to_critical"), _CAP_DISTANCE
    )
    for lens in structured.get("per_lens") or []:
        lens["observation"] = _cap_text(lens.get("observation"), _CAP_OBS)

    structured["confidence"] = normalize_confidence(structured.get("confidence"))

    logger.info(
        "Analysis: status=%s level_index=%s confidence=%.2f",
        structured.get("level_status"),
        structured.get("level_index"),
        structured.get("confidence") or 0.0,
    )
    return structured, input_tokens, output_tokens, cost_usd
