"""Integration tests: /settings/test-siren endpoint."""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest_web import (
    tmp_db, tmp_snap_dir, demo_db, app_env,
    get_session_cookie,
)


def reload_app():
    if "web.app" in sys.modules:
        del sys.modules["web.app"]
    import web.app
    return web.app.app


# ---------------------------------------------------------------------------
# /settings/test-siren — requires auth + CSRF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_siren_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/settings/test-siren", data={"csrf_token": "x"})
    # Unauthenticated → redirect to login or 401
    assert resp.status_code in (302, 401)


@pytest.mark.asyncio
async def test_siren_requires_csrf(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)
        resp = await c.post("/settings/test-siren", data={"csrf_token": "bad_token"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_siren_test_calls_publish_twice(app_env, monkeypatch):
    """POST /settings/test-siren must call publish({alarm:True}) then publish({alarm:False})."""
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env

    # Configure MQTT settings
    conn = wlm_db.connect(db_file)
    # Disabled on purpose: the test button must work before the siren is enabled.
    wlm_db.set_setting("siren_enabled", "0", conn=conn)
    wlm_db.set_setting("mqtt_host", "mqtt.local", conn=conn)
    conn.close()

    publish_calls = []

    def mock_publish(payload, conn=None, force=False):
        assert force is True
        publish_calls.append(payload)
        return (True, None)

    # Patch wlm.siren.publish and asyncio.sleep
    import wlm.siren as wlm_siren_mod
    monkeypatch.setattr(wlm_siren_mod, "publish", mock_publish)

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookies = await get_session_cookie(c)
        c.cookies.update(cookies)

        # Get CSRF token from settings page
        resp = await c.get("/settings")
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        assert m, "CSRF token not found in settings page"
        csrf = m.group(1)

        with patch("asyncio.sleep", new_callable=lambda: lambda *a, **k: __import__("asyncio").coroutine(lambda: None)()):
            # Use a real coroutine that returns immediately
            import asyncio

            async def fast_sleep(_):
                pass

            with patch("web.app.asyncio.sleep", fast_sleep):
                resp = await c.post(
                    "/settings/test-siren",
                    data={"csrf_token": csrf},
                    follow_redirects=True,
                )

    assert resp.status_code == 200
    assert len(publish_calls) == 2
    assert publish_calls[0] == {"alarm": True}
    assert publish_calls[1] == {"alarm": False}


@pytest.mark.asyncio
async def test_siren_test_sends_off_even_when_on_fails(app_env, monkeypatch):
    """An ON that reports failure may still reach the siren, so OFF is always sent."""
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("mqtt_host", "mqtt.local", conn=conn)
    conn.close()

    publish_calls = []

    def mock_publish(payload, conn=None, force=False):
        publish_calls.append(payload)
        return (False, "publish timed out") if payload["alarm"] else (True, None)

    import wlm.siren as wlm_siren_mod
    monkeypatch.setattr(wlm_siren_mod, "publish", mock_publish)

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.get("/settings")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)

        async def fast_sleep(_):
            pass

        with patch("web.app.asyncio.sleep", fast_sleep):
            await c.post("/settings/test-siren", data={"csrf_token": csrf}, follow_redirects=True)

    assert publish_calls == [{"alarm": True}, {"alarm": False}]


# ---------------------------------------------------------------------------
# /siren/stop — manual stop button
# ---------------------------------------------------------------------------

async def _csrf(c):
    c.cookies.update(await get_session_cookie(c))
    resp = await c.get("/settings")
    return re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text).group(1)


@pytest.mark.asyncio
async def test_stop_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/siren/stop", data={"csrf_token": "x"},
                            headers={"Accept": "application/json"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stop_requires_csrf(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.post("/siren/stop", data={"csrf_token": "bad"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stop_json_forces_off_and_logs(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db
    import wlm.siren as wlm_siren_mod

    calls = []

    def fake_stop(conn=None, force=False):
        calls.append(force)
        return True, None

    monkeypatch.setattr(wlm_siren_mod, "stop", fake_stop)
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/stop", data={"csrf_token": csrf},
                            headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "error": None}
    assert calls == [True]
    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    rows = conn.execute("SELECT message, delivered FROM alerts WHERE kind='siren'").fetchall()
    conn.close()
    assert [(r["message"], r["delivered"]) for r in rows] == [("Siren stopped from dashboard", 1)]


@pytest.mark.asyncio
async def test_stop_form_redirects_with_error_flash(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    import wlm.siren as wlm_siren_mod

    monkeypatch.setattr(wlm_siren_mod, "stop", lambda conn=None, force=False: (False, "MQTT connect failed: Not authorized"))
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/stop", data={"csrf_token": csrf}, follow_redirects=True)
    assert resp.status_code == 200
    assert "Siren stop failed: MQTT connect failed: Not authorized" in resp.text


@pytest.mark.asyncio
async def test_overview_shows_stop_button_only_with_mqtt_host(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        without = (await c.get("/")).text
        conn = wlm_db.connect(db_file)
        wlm_db.set_setting("mqtt_host", "mqtt.local", conn=conn)
        conn.close()
        with_host = (await c.get("/")).text
    assert "siren-stop-form" not in without
    assert "siren-stop-form" in with_host
