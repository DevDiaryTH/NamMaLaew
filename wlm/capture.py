"""Snapshot capture, image metrics, composite building, and housekeeping."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from wlm import db

logger = logging.getLogger("wlm.capture")


def mask_password(text: str) -> str:
    """Replace the password component in rtsp:// URLs (or any surrounding text) with ****."""
    return re.sub(r"(rtsp://[^:@/\s]+:)[^@\s]+(@)", r"\1****\2", text)


def parse_cam_streams() -> list[tuple[str, str]]:
    """Parse CAM_STREAMS env var into (label, url) pairs.

    Falls back to CAM_RTSP_URL as label "camera" when CAM_STREAMS is empty.
    """
    streams_env = os.getenv("CAM_STREAMS", "").strip()
    if streams_env:
        result: list[tuple[str, str]] = []
        for item in streams_env.split(","):
            item = item.strip()
            if not item:
                continue
            label, _sep, url = item.partition("=")
            label = label.strip()
            url = url.strip()
            if label and url:
                result.append((label, url))
                logger.debug("Stream: label=%s url=%s", label, mask_password(url))
        return result
    url = os.getenv("CAM_RTSP_URL", "").strip()
    if url:
        logger.debug("Using CAM_RTSP_URL fallback: %s", mask_password(url))
        return [("camera", url)]
    return []


def _downscale_image(path: Path, max_edge: int = 1568) -> None:
    """Resize the image in-place so its longest edge is at most max_edge pixels."""
    img = Image.open(path)
    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(path, "JPEG", quality=85)
        logger.debug("Downscaled %s: %dx%d → %dx%d", path.name, w, h, new_w, new_h)


# Real frames from this camera measure luma stddev ~55-75; undecodable gray frames measure < 3.
MIN_FRAME_CONTRAST = 8.0


def frame_contrast(path: Path) -> float:
    """Standard deviation of luma; near zero for a flat (gray/black) frame."""
    with Image.open(path) as img:
        return ImageStat.Stat(img.convert("L")).stddev[0]


def capture_snapshot(url: str, label: str = "camera") -> Path | None:
    """Grab one JPEG from url via ffmpeg (tcp, 20s timeout, 1 retry).

    Returns the Path of the saved file relative info is stored separately; the
    absolute path is returned here. Returns None on failure.
    """
    masked = mask_password(url)
    snap_dir = db.snapshot_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = snap_dir / f"{timestamp}-{label}.jpg"

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        # Decode keyframes only: the first HEVC frame from go2rtc often lacks its reference
        # frames and decodes to a flat gray image.
        "-skip_frame", "nokey",
        "-i", url,
        "-frames:v", "1",
        "-q:v", "2",
        "-y",
        str(out),
    ]

    for attempt in range(1, 3):
        logger.info("Capturing snapshot [%s] from %s (attempt %d/2)", label, masked, attempt)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=20, text=True)
            if proc.returncode == 0 and out.exists():
                contrast = frame_contrast(out)
                if contrast < MIN_FRAME_CONTRAST:
                    logger.warning(
                        "Rejected blank frame [%s] (luma stddev %.1f < %.1f) on attempt %d",
                        label, contrast, MIN_FRAME_CONTRAST, attempt,
                    )
                    out.unlink(missing_ok=True)
                    continue
                _downscale_image(out)
                logger.info("Snapshot saved: %s", out)
                return out
            stderr_masked = mask_password(proc.stderr.replace(url, masked))
            logger.warning("ffmpeg exited %d: %s", proc.returncode, stderr_masked[:300])
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out on attempt %d [%s]", attempt, label)
        except Exception as exc:
            logger.error("Unexpected error on attempt %d [%s]: %s", attempt, label, exc)

    logger.error("All snapshot attempts failed for %s [%s]", masked, label)
    return None


def image_metrics(path: Path) -> dict:
    """Compute per-frame quality metrics.

    Returns:
        brightness: mean luma 0-255
        sharpness: variance of luma after FIND_EDGES filter (Laplacian-like)
        is_night: True when mean HSV saturation < 12 (IR grayscale mode)
    """
    img = Image.open(path).convert("RGB")

    # Luma (Y channel) from RGB
    luma = img.convert("L")
    pixels = list(luma.getdata())
    n = len(pixels)
    mean_luma = sum(pixels) / n if n else 0.0

    # Edge variance for sharpness
    edges = luma.filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(edges.getdata())
    mean_e = sum(edge_pixels) / n if n else 0.0
    variance = sum((p - mean_e) ** 2 for p in edge_pixels) / n if n else 0.0

    # HSV saturation for IR detection
    hsv = img.convert("HSV")
    _, s, _ = hsv.split()
    sat_pixels = list(s.getdata())
    mean_sat = sum(sat_pixels) / len(sat_pixels) if sat_pixels else 0.0

    return {
        "brightness": mean_luma,
        "sharpness": variance,
        "is_night": mean_sat < 12,
    }


def build_composite(frames: list[tuple[str, Path]], max_width: int = 1280) -> Path:
    """Stack frames vertically into a single composite JPEG with lens labels."""
    if not frames:
        raise ValueError("Cannot build composite from zero frames")

    snap_dir = db.snapshot_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)

    opened: list[tuple[str, Image.Image]] = []
    for label, path in frames:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        opened.append((label, img))

    total_width = max(img.size[0] for _, img in opened)
    total_height = sum(img.size[1] for _, img in opened)

    composite = Image.new("RGB", (total_width, total_height), (0, 0, 0))
    draw = ImageDraw.Draw(composite)

    font: ImageFont.ImageFont | ImageFont.FreeTypeFont = ImageFont.load_default()
    for font_path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(font_path, 22)
            break
        except Exception:
            pass

    y_offset = 0
    for label, img in opened:
        composite.paste(img, (0, y_offset))
        draw.text((9, y_offset + 9), label, fill=(0, 0, 0), font=font)
        draw.text((8, y_offset + 8), label, fill=(255, 255, 0), font=font)
        y_offset += img.size[1]

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = snap_dir / f"{timestamp}-composite.jpg"
    composite.save(out_path, "JPEG", quality=85)
    logger.info("Composite saved: %s", out_path.name)
    return out_path


def prune_snapshots() -> None:
    """Delete JPEG files in snapshot_dir older than SNAPSHOT_RETENTION_HOURS (default 168)."""
    try:
        retention_hours = int(os.getenv("SNAPSHOT_RETENTION_HOURS", "168"))
    except ValueError:
        retention_hours = 168
    cutoff = datetime.now() - timedelta(hours=retention_hours)
    snap_dir = db.snapshot_dir()
    for f in snap_dir.glob("*.jpg"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                logger.debug("Pruned old snapshot: %s", f.name)
        except OSError as exc:
            logger.warning("Could not prune %s: %s", f, exc)
