"""Telegram notification helpers.

Contract used by the dashboard — keep exact signatures.
Never log the bot token.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("wlm.telegram")

_MAX_CAPTION = 1024


def _truncate_caption(caption: str) -> str:
    if len(caption) <= _MAX_CAPTION:
        return caption
    return caption[: _MAX_CAPTION - 3] + "..."


def send_message(token: str, chat_id: str, text: str) -> tuple[bool, str | None]:
    """POST a plain-text message to Telegram sendMessage.

    Returns (True, None) on success, (False, error_text) on failure.
    The token is never logged.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            logger.info("Telegram message sent to chat %s", chat_id)
            return True, None
        err = str(result.get("description", result))
        logger.error("Telegram sendMessage error: %s", err)
        return False, err
    except Exception as exc:
        err = str(exc)
        logger.error("Failed to send Telegram message: %s", err)
        return False, err


def _build_multipart(boundary: str, chat_id: str, caption: str, photo: bytes) -> bytes:
    CRLF = b"\r\n"
    B = boundary.encode()

    def text_field(name: str, value: str) -> bytes:
        return (
            b"--" + B + CRLF
            + f'Content-Disposition: form-data; name="{name}"'.encode() + CRLF
            + CRLF
            + value.encode("utf-8") + CRLF
        )

    return (
        text_field("chat_id", chat_id)
        + text_field("caption", caption)
        + b"--" + B + CRLF
        + b'Content-Disposition: form-data; name="photo"; filename="snapshot.jpg"' + CRLF
        + b"Content-Type: image/jpeg" + CRLF
        + CRLF
        + photo + CRLF
        + b"--" + B + b"--" + CRLF
    )


def send_photo(
    token: str, chat_id: str, image_path: Path, caption: str
) -> tuple[bool, str | None]:
    """POST image_path to Telegram sendPhoto.

    Returns (True, None) on success, (False, error_text) on failure.
    Caption is truncated to 1024 chars. The token is never logged.
    """
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    boundary = f"WaterMon{int(time.time())}"
    caption = _truncate_caption(caption)
    body = _build_multipart(boundary, chat_id, caption, image_path.read_bytes())
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            logger.info("Telegram photo sent to chat %s", chat_id)
            return True, None
        err = str(data.get("description", data))
        logger.error("Telegram sendPhoto error: %s", err)
        return False, err
    except Exception as exc:
        err = str(exc)
        logger.error("Failed to send Telegram photo: %s", err)
        return False, err
