"""User-drawn alert lines per lens, stored as JSON in the settings table (key "level_lines").

Shape (coordinates normalized to 0..1 of image width/height, origin top-left):
    {
      "street":  {"warning": [[x, y], [x, y], ...], "critical": [[x, y], ...]},
      "carport": {"critical": [[x, y], [x, y]]}
    }
Each line is a polyline with at least 2 points; either kind may be absent for a lens.
"""

from __future__ import annotations

import json

from wlm import db

SETTING_KEY = "level_lines"
KINDS = ("warning", "critical")


def validate(lines: object) -> dict[str, dict[str, list[list[float]]]]:
    """Return a cleaned copy of *lines*, or raise ValueError describing the problem."""
    if not isinstance(lines, dict):
        raise ValueError("lines must be an object keyed by lens label")
    cleaned: dict[str, dict[str, list[list[float]]]] = {}
    for label, per_kind in lines.items():
        if not isinstance(label, str) or not label or len(label) > 64:
            raise ValueError(f"invalid lens label: {label!r}")
        if not isinstance(per_kind, dict):
            raise ValueError(f"lens {label!r}: expected an object of kind -> points")
        out: dict[str, list[list[float]]] = {}
        for kind, points in per_kind.items():
            if kind not in KINDS:
                raise ValueError(f"lens {label!r}: unknown line kind {kind!r}")
            if not isinstance(points, list) or not 2 <= len(points) <= 50:
                raise ValueError(f"lens {label!r} {kind}: need 2-50 points")
            pts = []
            for p in points:
                if (not isinstance(p, (list, tuple)) or len(p) != 2
                        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
                    raise ValueError(f"lens {label!r} {kind}: point must be [x, y]")
                x, y = float(p[0]), float(p[1])
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    raise ValueError(f"lens {label!r} {kind}: coordinates must be within 0..1")
                pts.append([round(x, 4), round(y, 4)])
            out[kind] = pts
        if out:
            cleaned[label] = out
    return cleaned


def get_lines(conn=None) -> dict[str, dict[str, list[list[float]]]]:
    raw = db.get_setting(SETTING_KEY, conn=conn)
    if not raw:
        return {}
    try:
        return validate(json.loads(raw))
    except (ValueError, json.JSONDecodeError):
        return {}


def set_lines(lines: object, conn=None) -> dict[str, dict[str, list[list[float]]]]:
    cleaned = validate(lines)
    db.set_setting(SETTING_KEY, json.dumps(cleaned, separators=(",", ":")), conn=conn)
    return cleaned
