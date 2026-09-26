"""Draw user-drawn alert lines onto a lens frame before sending to Claude.

Coordinates are normalized (0..1), origin top-left.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Colours
_COLOUR_WARNING = (245, 158, 11)   # #f59e0b amber
_COLOUR_CRITICAL = (239, 68, 68)   # #ef4444 red
_COLOUR_OUTLINE = (30, 30, 30)     # dark outline for contrast

# Dash pattern: (drawn_px, gap_px)
_DASH_DRAWN = 18
_DASH_GAP = 9


def _get_font(size: int = 13) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for font_path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _scale_points(
    points: list[list[float]], width: int, height: int
) -> list[tuple[int, int]]:
    return [(int(x * width), int(y * height)) for x, y in points]


def _draw_dashed_polyline(
    draw: ImageDraw.ImageDraw,
    pts: list[tuple[int, int]],
    colour: tuple[int, int, int],
    line_width: int,
) -> None:
    """Draw a dashed polyline.  Each segment is split into alternating
    drawn/gap sections using linear interpolation."""
    if len(pts) < 2:
        return

    outline_w = max(1, line_width + 2)

    def _draw_segments(col: tuple[int, int, int], lw: int) -> None:
        drawn_remaining = 0.0
        gap_remaining = 0.0
        in_gap = False
        carry_x, carry_y = pts[0]

        for i in range(1, len(pts)):
            x0, y0 = carry_x, carry_y
            x1, y1 = pts[i]
            seg_len = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
            if seg_len == 0:
                continue

            pos = 0.0
            while pos < seg_len:
                if in_gap:
                    step = min(gap_remaining if gap_remaining > 0 else _DASH_GAP, seg_len - pos)
                    pos += step
                    gap_remaining = max(0.0, (gap_remaining if gap_remaining > 0 else _DASH_GAP) - step)
                    if gap_remaining == 0:
                        in_gap = False
                        drawn_remaining = _DASH_DRAWN
                else:
                    step = min(drawn_remaining if drawn_remaining > 0 else _DASH_DRAWN, seg_len - pos)
                    t0 = pos / seg_len
                    t1 = (pos + step) / seg_len
                    sx0 = int(x0 + t0 * (x1 - x0))
                    sy0 = int(y0 + t0 * (y1 - y0))
                    sx1 = int(x0 + t1 * (x1 - x0))
                    sy1 = int(y0 + t1 * (y1 - y0))
                    draw.line([(sx0, sy0), (sx1, sy1)], fill=col, width=lw)
                    pos += step
                    drawn_remaining = max(0.0, (drawn_remaining if drawn_remaining > 0 else _DASH_DRAWN) - step)
                    if drawn_remaining == 0:
                        in_gap = True
                        gap_remaining = _DASH_GAP
            carry_x, carry_y = x1, y1

    # Draw dark outline first, then the coloured line on top
    _draw_segments(_COLOUR_OUTLINE, outline_w)
    _draw_segments(colour, line_width)


def _draw_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    px: int,
    py: int,
    colour: tuple[int, int, int],
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Draw a filled rectangle with the text on top, near (px, py)."""
    padding = 3
    # Measure text size
    try:
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except AttributeError:
        tw, th = draw.textsize(text, font=font)  # type: ignore[arg-type]

    rx0 = px + 4
    ry0 = py - th - padding * 2
    if ry0 < 0:
        ry0 = py + 4
    rx1 = rx0 + tw + padding * 2
    ry1 = ry0 + th + padding * 2

    draw.rectangle([rx0, ry0, rx1, ry1], fill=colour)
    draw.text((rx0 + padding, ry0 + padding), text, fill=(255, 255, 255), font=font)


def draw_lines(src: Path, lens_lines: dict, dst: Path) -> Path:
    """Draw warning/critical lines from *lens_lines* onto *src*, writing the
    result to *dst*.  Returns *dst*.

    *lens_lines* is the per-lens dict from wlm.lines.get_lines() for this lens,
    e.g. {"warning": [[x,y],...], "critical": [[x,y],...]}.

    If *lens_lines* is empty (no keys), *src* is copied unchanged to *dst* and
    *dst* is returned.  *src* is never modified.
    """
    if not lens_lines:
        shutil.copy2(src, dst)
        return dst

    img = Image.open(src).convert("RGB")
    width, height = img.size

    line_width = max(3, width // 300)
    font = _get_font(max(12, width // 80))

    draw = ImageDraw.Draw(img)

    kind_order = [("warning", _COLOUR_WARNING, "WARNING"),
                  ("critical", _COLOUR_CRITICAL, "CRITICAL")]

    for kind, colour, label_text in kind_order:
        points_norm = lens_lines.get(kind)
        if not points_norm:
            continue
        pts = _scale_points(points_norm, width, height)
        _draw_dashed_polyline(draw, pts, colour, line_width)
        # Label near the first point
        _draw_label(draw, label_text, pts[0][0], pts[0][1], colour, font)

    img.save(dst, "JPEG", quality=85)
    return dst
