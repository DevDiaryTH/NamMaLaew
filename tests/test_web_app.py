"""Integration tests for the web dashboard (auth, pages, APIs, security)."""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path
from datetime import datetime, timezone

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

# Shared fixtures
from tests.conftest_web import (
    tmp_db, tmp_snap_dir, demo_db, app_env, no_password_env,
    make_client, get_session_cookie,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def reload_app():
    """Reload web.app to pick up env var changes."""
    if "web.app" in sys.modules:
        del sys.modules["web.app"]
    import web.app
    return web.app.app


async def authenticated_client(app_env):
    """Return an AsyncClient with a valid session cookie."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    cookies = await get_session_cookie(client, password="testpass")
    client.cookies.update(cookies)
    return client


# ---------------------------------------------------------------------------
# /healthz (no auth required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz_no_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "db" in data


# ---------------------------------------------------------------------------
# No password configured → setup page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_password_shows_setup(no_password_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/")
        assert resp.status_code == 503
        assert "DASHBOARD_PASSWORD" in resp.text


@pytest.mark.asyncio
async def test_no_password_api_returns_503(no_password_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/summary")
        assert resp.status_code in (401, 503)


# ---------------------------------------------------------------------------
# Auth required on pages, APIs, snapshots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_root_redirects_to_login(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_timeline_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/timeline", follow_redirects=False)
        assert resp.status_code in (302, 401)


@pytest.mark.asyncio
async def test_alerts_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/alerts", follow_redirects=False)
        assert resp.status_code in (302, 401)


@pytest.mark.asyncio
async def test_settings_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/settings", follow_redirects=False)
        assert resp.status_code in (302, 401)


@pytest.mark.asyncio
async def test_api_summary_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/summary")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_series_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/series")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_snapshot_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/snapshots/test.jpg")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Login success and failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c, password="testpass")
        assert cookies  # got a session cookie


@pytest.mark.asyncio
async def test_login_wrong_password(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Get login page to get CSRF
        resp = await c.get("/login")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        assert m
        csrf = m.group(1)
        login_resp = await c.post(
            "/login",
            data={"password": "wrongpassword", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert login_resp.status_code == 401
        assert "Incorrect" in login_resp.text


# ---------------------------------------------------------------------------
# CSRF rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_bad_csrf_rejected(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/login",
            data={"password": "testpass", "csrf_token": "bad_csrf_token"},
            follow_redirects=False,
        )
        assert resp.status_code in (400, 401)


@pytest.mark.asyncio
async def test_settings_post_bad_csrf_rejected(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Login first
        cookies = await get_session_cookie(c, password="testpass")
        c.cookies.update(cookies)
        resp = await c.post(
            "/settings",
            data={"csrf_token": "invalid_csrf"},
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Pages work when authenticated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_authenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/")
        assert resp.status_code == 200
        assert "Water Level" in resp.text or "WATER LEVEL" in resp.text or "WLM" in resp.text


@pytest.mark.asyncio
async def test_timeline_authenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/timeline")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_alerts_authenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/alerts")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_settings_authenticated(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/settings")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings: save + validation errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_validation_warning_gte_critical(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        # Get CSRF from settings page
        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        assert m
        csrf = m.group(1)

        # level_warning >= level_critical → validation error
        resp = await c.post(
            "/settings",
            data={
                "csrf_token": csrf,
                "level_warning": "90",
                "level_critical": "50",
                "capture_interval_minutes": "10",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 422
        assert "less than" in resp.text.lower() or "warning" in resp.text.lower()


@pytest.mark.asyncio
async def test_settings_save_ok(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)

        post_data = {
            "csrf_token": csrf,
            "level_warning": "45",
            "level_critical": "85",
            "capture_interval_minutes": "15",
            "telegram_enabled": "1",
            "telegram_chat_id": "999888",
            "alert_on_warning": "1",
            "alert_on_recovery": "1",
            "alert_on_failure": "1",
            "failure_threshold": "3",
            "critical_repeat_minutes": "10",
            "heartbeat_hour": "",
            "claude_model": "claude-opus-5",
            "reference_description": "test",
            "latitude": "",
            "longitude": "",
        }
        resp = await c.post("/settings", data=post_data, follow_redirects=True)
        assert resp.status_code == 200
        assert "saved" in resp.text.lower() or "settings" in resp.text.lower()


# ---------------------------------------------------------------------------
# Secret masking: token never appears in response body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secret_not_in_settings_page(app_env, tmp_db):
    """telegram_bot_token must never appear in full in any response."""
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    _, db_file = tmp_db
    # We use the app_env fixture which has a separate DB — check its DB
    db_path_str = str(app_env[0])

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # First set a token via settings
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)

        secret_token = "super_secret_bot_token_12345678"
        post_data = {
            "csrf_token": csrf,
            "level_warning": "45",
            "level_critical": "85",
            "capture_interval_minutes": "10",
            "telegram_bot_token": secret_token,
        }
        await c.post("/settings", data=post_data, follow_redirects=True)

        # Now check that the full token does not appear in the response
        resp = await c.get("/settings")
        assert secret_token not in resp.text, "Token must not appear in settings page HTML"

        # Check API too
        resp_api = await c.get("/api/summary")
        assert secret_token not in resp_api.text


# ---------------------------------------------------------------------------
# Secret field: blank keeps value, explicit clear removes it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secret_blank_keeps_value(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("telegram_bot_token", "keep_this_token", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)

        # Submit with blank token (no clear checkbox)
        post_data = {
            "csrf_token": csrf,
            "level_warning": "45",
            "level_critical": "85",
            "capture_interval_minutes": "10",
            "telegram_bot_token": "",  # blank → keep
        }
        await c.post("/settings", data=post_data, follow_redirects=True)

        # Verify the token is still in DB
        conn2 = wlm_db.connect(db_file)
        stored = wlm_db.get_setting("telegram_bot_token", conn=conn2)
        conn2.close()
        assert stored == "keep_this_token"


@pytest.mark.asyncio
async def test_secret_clear_removes_value(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("telegram_bot_token", "remove_this_token", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)

        post_data = {
            "csrf_token": csrf,
            "level_warning": "45",
            "level_critical": "85",
            "capture_interval_minutes": "10",
            "telegram_bot_token": "",
            "telegram_bot_token_clear": "1",  # explicit clear
        }
        await c.post("/settings", data=post_data, follow_redirects=True)

        conn2 = wlm_db.connect(db_file)
        stored = wlm_db.get_setting("telegram_bot_token", conn=conn2)
        conn2.close()
        assert stored in ("", None)


# ---------------------------------------------------------------------------
# Test Telegram button: calls mock and records alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_button_calls_mock(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env

    # Set up a saved token and chat_id
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("telegram_bot_token", "mock_token", conn=conn)
    wlm_db.set_setting("telegram_chat_id", "123", conn=conn)
    conn.close()

    # Inject mock telegram module
    call_log = []

    def mock_send_message(token, chat_id, text):
        call_log.append((token, chat_id, text))
        return (True, None)

    mock_tg = types.ModuleType("wlm.telegram")
    mock_tg.send_message = mock_send_message
    sys.modules["wlm.telegram"] = mock_tg

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = m.group(1)

        resp = await c.post(
            "/settings/test-telegram",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    # Verify mock was called
    assert len(call_log) == 1
    # Token should be the mock_token (not visible in HTML)
    assert call_log[0][0] == "mock_token"

    # Verify alert was recorded
    conn2 = wlm_db.connect(db_file)
    alerts = conn2.execute("SELECT * FROM alerts WHERE kind='test' ORDER BY ts DESC LIMIT 1").fetchone()
    conn2.close()
    assert alerts is not None
    assert alerts["delivered"] == 1

    # Cleanup
    del sys.modules["wlm.telegram"]


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_traversal_dotdot(app_env, tmp_snap_dir):
    from httpx import AsyncClient, ASGITransport

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/snapshots/../etc/passwd")
        assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_path_traversal_encoded(app_env):
    from httpx import AsyncClient, ASGITransport

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        # URL-encoded traversal
        resp = await c.get("/snapshots/%2e%2e%2fetc%2fpasswd")
        assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# JSON API: summary returns key fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_summary_returns_fields(app_env):
    from httpx import AsyncClient, ASGITransport

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "level_critical" in data
        assert "level_warning" in data


@pytest.mark.asyncio
async def test_api_series_returns_readings(app_env):
    from httpx import AsyncClient, ASGITransport

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/api/series?range=3d")
        assert resp.status_code == 200
        data = resp.json()
        assert "readings" in data
        assert isinstance(data["readings"], list)


@pytest.mark.asyncio
async def test_login_non_ascii_password(app_env, monkeypatch):
    """hmac.compare_digest raises TypeError on non-ASCII str; passwords must be compared as bytes."""
    from httpx import AsyncClient, ASGITransport
    monkeypatch.setenv("DASHBOARD_PASSWORD", "น้ำท่วม123")
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/login")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)
        bad = await c.post("/login", data={"password": "ผิด", "csrf_token": csrf})
        assert bad.status_code == 401
        good = await c.post("/login", data={"password": "น้ำท่วม123", "csrf_token": csrf})
        assert good.status_code == 302


@pytest.mark.asyncio
async def test_forwarded_for_header_does_not_reset_lockout(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/login")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)
        for i in range(10):
            await c.post("/login", data={"password": "x", "csrf_token": csrf},
                         headers={"X-Forwarded-For": f"10.0.0.{i}"})
        blocked = await c.post("/login", data={"password": "testpass", "csrf_token": csrf},
                               headers={"X-Forwarded-For": "10.9.9.9"})
        assert blocked.status_code == 429


# ---------------------------------------------------------------------------
# /lines page — requires auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lines_page_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/lines", follow_redirects=False)
        assert resp.status_code in (302, 401)


@pytest.mark.asyncio
async def test_lines_page_renders_with_auth(app_env):
    """Lines page renders canvas for lens with snapshot, message for lens without."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/lines")
        assert resp.status_code == 200
        # Street has a snapshot → canvas should appear
        assert "canvas" in resp.text.lower() or "street" in resp.text.lower()
        # Carport has no snapshot → message should appear
        assert "carport" in resp.text.lower()
        # Help text should be present
        assert "warning" in resp.text.lower()


# ---------------------------------------------------------------------------
# GET /api/lines — requires auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_lines_get_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/lines")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_lines_get_returns_dict(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/lines")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)  # {} when no lines set


# ---------------------------------------------------------------------------
# POST /api/lines — auth + CSRF required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_lines_post_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/lines", json={"street": {"warning": [[0.1, 0.5], [0.9, 0.5]]}})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_lines_post_requires_csrf(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        # No CSRF header → should be rejected
        resp = await c.post(
            "/api/lines",
            json={"street": {"warning": [[0.1, 0.5], [0.9, 0.5]]}},
            headers={"X-CSRF-Token": "wrong_csrf"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_api_lines_post_valid_roundtrip(app_env):
    """Valid payload saves and can be read back."""
    from httpx import AsyncClient, ASGITransport
    from wlm import lines as wlm_lines
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        # Get CSRF token from lines page
        resp = await c.get("/lines")
        csrf = re.search(r'"([0-9a-f]{32})"', resp.text)
        # Extract CSRF from any page
        login_page = await c.get("/settings")
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text)
        assert csrf_m
        csrf_token = csrf_m.group(1)

        payload = {
            "street": {
                "warning": [[0.1, 0.5], [0.5, 0.5], [0.9, 0.5]],
                "critical": [[0.1, 0.3], [0.5, 0.3], [0.9, 0.3]],
            }
        }
        resp = await c.post(
            "/api/lines",
            json=payload,
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "street" in data
        assert "warning" in data["street"]
        assert len(data["street"]["warning"]) == 3

        # Read back
        get_resp = await c.get("/api/lines")
        get_data = get_resp.json()
        assert "street" in get_data
        assert len(get_data["street"]["warning"]) == 3


@pytest.mark.asyncio
async def test_api_lines_post_invalid_one_point(app_env):
    """Payload with only 1 point → 400 with message."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)

        resp = await c.post(
            "/api/lines",
            json={"street": {"warning": [[0.1, 0.5]]}},  # only 1 point
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data


@pytest.mark.asyncio
async def test_api_lines_post_invalid_out_of_range(app_env):
    """Coordinates outside 0..1 → 400 with message."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)

        resp = await c.post(
            "/api/lines",
            json={"street": {"warning": [[1.5, 0.5], [0.9, 0.5]]}},  # x > 1.0
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data


@pytest.mark.asyncio
async def test_api_lines_post_invalid_unknown_kind(app_env):
    """Unknown line kind → 400 with message."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        resp = await c.get("/settings")
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)

        resp = await c.post(
            "/api/lines",
            json={"street": {"danger": [[0.1, 0.5], [0.9, 0.5]]}},  # unknown kind
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data


# ---------------------------------------------------------------------------
# /api/image-series — auth required, returns brightness/sharpness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_image_series_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/image-series")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_image_series_returns_fields(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/api/image-series?range=7d")
        assert resp.status_code == 200
        data = resp.json()
        assert "lenses" in data
        assert "series" in data
        assert "latest" in data
        # The demo_db has lens readings with brightness data for "street"
        if "street" in data["lenses"]:
            street_series = data["series"].get("street", [])
            if street_series:
                assert "brightness" in street_series[0]
                assert "sharpness" in street_series[0]
                assert "is_night" in street_series[0]


# ---------------------------------------------------------------------------
# line_position badge rendered on overview and timeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_line_position_badge_rendered(app_env):
    """Overview page renders line_position badge when value is set."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/")
        assert resp.status_code == 200
        # The demo_db has line_position values set; badge CSS classes should appear
        assert "badge-lp" in resp.text


@pytest.mark.asyncio
async def test_timeline_line_position_badge_rendered(app_env):
    """Timeline page renders line_position badge indicators."""
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.get("/timeline")
        assert resp.status_code == 200
        # Some readings have line_position, so badge classes should appear
        assert "badge-lp" in resp.text
