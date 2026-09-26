"""Tests: telegram token never appears in logs, send_message/send_photo signatures."""

from __future__ import annotations

import json
import logging
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

from wlm.telegram import send_message, send_photo


def _make_photo(path: Path) -> None:
    img = Image.new("RGB", (50, 50), (100, 100, 100))
    img.save(str(path), "JPEG")


def _mock_urlopen(ok: bool = True, description: str = "OK"):
    body = json.dumps({"ok": ok, "description": description}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestSendMessage:
    def test_returns_true_on_success(self):
        with patch("wlm.telegram.urllib.request.urlopen", return_value=_mock_urlopen(True)):
            ok, err = send_message("FAKE_TOKEN", "12345", "hello")
        assert ok is True
        assert err is None

    def test_returns_false_on_api_error(self):
        with patch("wlm.telegram.urllib.request.urlopen", return_value=_mock_urlopen(False, "bad chat")):
            ok, err = send_message("FAKE_TOKEN", "12345", "hello")
        assert ok is False
        assert err is not None

    def test_returns_false_on_network_error(self):
        with patch("wlm.telegram.urllib.request.urlopen", side_effect=Exception("timeout")):
            ok, err = send_message("FAKE_TOKEN", "12345", "hello")
        assert ok is False
        assert err is not None


class TestSendPhoto:
    def test_returns_true_on_success(self, tmp_path):
        p = tmp_path / "photo.jpg"
        _make_photo(p)
        with patch("wlm.telegram.urllib.request.urlopen", return_value=_mock_urlopen(True)):
            ok, err = send_photo("FAKE_TOKEN", "12345", p, "caption")
        assert ok is True
        assert err is None

    def test_caption_truncated_to_1024(self, tmp_path):
        p = tmp_path / "photo.jpg"
        _make_photo(p)
        long_caption = "x" * 2000
        captured_body = {}

        def fake_urlopen(req, timeout=None):
            captured_body["body"] = req.data
            return _mock_urlopen(True)

        with patch("wlm.telegram.urllib.request.urlopen", side_effect=fake_urlopen):
            send_photo("FAKE_TOKEN", "12345", p, long_caption)

        body_str = captured_body["body"].decode("utf-8", errors="replace")
        # Caption in body should be at most 1024 chars
        assert "x" * 1025 not in body_str


class TestTokenNotLogged:
    def test_token_not_in_log_output(self, caplog, tmp_path):
        """The bot token must never appear in any log output."""
        SECRET_TOKEN = "VERY_SECRET_BOT_TOKEN_12345"
        p = tmp_path / "photo.jpg"
        _make_photo(p)

        with caplog.at_level(logging.DEBUG, logger="wlm.telegram"):
            with patch("wlm.telegram.urllib.request.urlopen",
                       side_effect=Exception("test error")):
                send_message(SECRET_TOKEN, "12345", "test")
                send_photo(SECRET_TOKEN, "12345", p, "test")

        for record in caplog.records:
            assert SECRET_TOKEN not in record.getMessage(), \
                f"Token found in log: {record.getMessage()}"
