"""Tests: image_metrics, capture helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PIL import Image

from wlm.capture import image_metrics, mask_password, parse_cam_streams


def _make_image(path: Path, mode: str = "RGB", color=(128, 128, 128), size=(100, 100)) -> None:
    img = Image.new(mode, size, color)
    if mode == "RGB":
        img.save(str(path), "JPEG")
    else:
        img.convert("RGB").save(str(path), "JPEG")


class TestImageMetrics:
    def test_bright_image(self, tmp_path):
        p = tmp_path / "bright.jpg"
        _make_image(p, color=(255, 255, 255))
        m = image_metrics(p)
        assert m["brightness"] > 200
        assert isinstance(m["sharpness"], float)
        assert isinstance(m["is_night"], bool)

    def test_dark_image(self, tmp_path):
        p = tmp_path / "dark.jpg"
        _make_image(p, color=(10, 10, 10))
        m = image_metrics(p)
        assert m["brightness"] < 50

    def test_colorful_not_night(self, tmp_path):
        """A colorful image has high saturation → is_night=False."""
        p = tmp_path / "color.jpg"
        # Strong red color → high saturation
        _make_image(p, color=(255, 0, 0))
        m = image_metrics(p)
        assert m["is_night"] is False

    def test_grayscale_is_night(self, tmp_path):
        """A pure grayscale image (zero saturation) → is_night=True."""
        p = tmp_path / "gray.jpg"
        # Pure grey: R=G=B means no saturation
        _make_image(p, color=(128, 128, 128))
        m = image_metrics(p)
        # Pure grey has 0 saturation in HSV
        assert m["is_night"] is True

    def test_sharp_vs_flat(self, tmp_path):
        """An image with edges has higher sharpness than a flat solid image."""
        flat_p = tmp_path / "flat.jpg"
        _make_image(flat_p, color=(100, 100, 100))
        m_flat = image_metrics(flat_p)

        # Create a checkerboard image (lots of edges)
        check_p = tmp_path / "check.jpg"
        img = Image.new("RGB", (100, 100))
        pixels = img.load()
        for i in range(100):
            for j in range(100):
                pixels[i, j] = (255, 255, 255) if (i + j) % 2 == 0 else (0, 0, 0)
        img.save(str(check_p), "JPEG")
        m_check = image_metrics(check_p)

        assert m_check["sharpness"] > m_flat["sharpness"]


class TestMaskPassword:
    def test_masks_rtsp_password(self):
        url = "rtsp://user:s3cr3t@192.168.1.1:554/stream"
        masked = mask_password(url)
        assert "s3cr3t" not in masked
        assert "****" in masked
        assert "user" in masked

    def test_no_password_unchanged(self):
        url = "rtsp://192.168.1.1:554/stream"
        assert mask_password(url) == url

    def test_masks_in_ffmpeg_stderr(self):
        url = "rtsp://admin:mypassword@192.168.1.1:554"
        text = f"ffmpeg: Could not open {url}: connection refused"
        masked = mask_password(text)
        assert "mypassword" not in masked


class TestParseCamStreams:
    def test_parse_two_streams(self, monkeypatch):
        monkeypatch.setenv("CAM_STREAMS", "street=rtsp://h:8554/ec6_2,carport=rtsp://h:8554/ec6")
        monkeypatch.delenv("CAM_RTSP_URL", raising=False)
        result = parse_cam_streams()
        assert len(result) == 2
        assert result[0] == ("street", "rtsp://h:8554/ec6_2")
        assert result[1] == ("carport", "rtsp://h:8554/ec6")

    def test_fallback_to_cam_rtsp_url(self, monkeypatch):
        monkeypatch.setenv("CAM_STREAMS", "")
        monkeypatch.setenv("CAM_RTSP_URL", "rtsp://127.0.0.1:8554/cam")
        result = parse_cam_streams()
        assert len(result) == 1
        assert result[0] == ("camera", "rtsp://127.0.0.1:8554/cam")

    def test_empty_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CAM_STREAMS", "")
        monkeypatch.delenv("CAM_RTSP_URL", raising=False)
        assert parse_cam_streams() == []

    def test_skips_empty_items(self, monkeypatch):
        monkeypatch.setenv("CAM_STREAMS", "a=rtsp://x,,b=rtsp://y")
        result = parse_cam_streams()
        assert len(result) == 2


def test_frame_contrast_flags_flat_gray_frame(tmp_path):
    from PIL import Image, ImageDraw
    from wlm.capture import frame_contrast, MIN_FRAME_CONTRAST

    gray = tmp_path / "gray.jpg"
    Image.new("RGB", (320, 180), (128, 128, 128)).save(gray)
    scene = tmp_path / "scene.jpg"
    img = Image.new("RGB", (320, 180), (20, 20, 20))
    ImageDraw.Draw(img).rectangle([80, 40, 240, 140], fill=(230, 230, 230))
    img.save(scene)

    assert frame_contrast(gray) < MIN_FRAME_CONTRAST
    assert frame_contrast(scene) > MIN_FRAME_CONTRAST


def test_capture_retries_when_first_frame_is_blank(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    import wlm.capture as cap

    monkeypatch.setenv("WLM_SNAPSHOT_DIR", str(tmp_path))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out = cmd[-1]
        if len(calls) == 1:
            Image.new("RGB", (320, 180), (128, 128, 128)).save(out)
        else:
            img = Image.new("RGB", (320, 180), (20, 20, 20))
            ImageDraw.Draw(img).rectangle([80, 40, 240, 140], fill=(230, 230, 230))
            img.save(out)

        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(cap.subprocess, "run", fake_run)
    path = cap.capture_snapshot("rtsp://127.0.0.1:8554/x", "street")
    assert path is not None and path.exists()
    assert len(calls) == 2
    assert "-skip_frame" in calls[0] and calls[0][calls[0].index("-skip_frame") + 1] == "nokey"
    assert calls[0].index("-skip_frame") < calls[0].index("-i")
