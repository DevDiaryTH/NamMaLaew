"""Tests: /siren/mute and /siren/unmute endpoints + UI rendering."""

from __future__ import annotations

import re
import sys
from pathlib import Path

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


async def _csrf(c):
    """Log in (if needed) and return a CSRF token from the settings page."""
    c.cookies.update(await get_session_cookie(c))
    resp = await c.get("/settings")
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    assert m, "CSRF token not found"
    return m.group(1)


# ---------------------------------------------------------------------------
# /siren/mute — auth + CSRF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mute_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/siren/mute", data={"csrf_token": "x"},
                            headers={"Accept": "application/json"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mute_requires_csrf(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.post("/siren/mute", data={"csrf_token": "bad"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /siren/unmute — auth + CSRF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unmute_requires_auth(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/siren/unmute", data={"csrf_token": "x"},
                            headers={"Accept": "application/json"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unmute_requires_csrf(app_env):
    from httpx import AsyncClient, ASGITransport
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.post("/siren/unmute", data={"csrf_token": "bad"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /siren/mute — JSON responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mute_json_sets_state_and_logs(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db
    import wlm.siren as wlm_siren_mod

    db_file, _ = app_env
    mute_calls = []

    def fake_mute(conn=None):
        mute_calls.append(True)
        return (True, None)

    monkeypatch.setattr(wlm_siren_mod, "mute", fake_mute)
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/mute", data={"csrf_token": csrf},
                            headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["muted"] is True
    assert body["ok"] is True
    assert mute_calls == [True]

    conn = wlm_db.connect(db_file)
    rows = conn.execute(
        "SELECT message, delivered FROM alerts WHERE kind='siren'"
    ).fetchall()
    conn.close()
    assert any("muted from dashboard" in r["message"].lower() for r in rows)


@pytest.mark.asyncio
async def test_mute_json_muted_true_even_when_stop_fails(app_env, monkeypatch):
    """muted=True is always returned: the state is set even when publish fails."""
    from httpx import AsyncClient, ASGITransport
    import wlm.siren as wlm_siren_mod

    def fake_mute(conn=None):
        return (False, "MQTT connect failed")

    monkeypatch.setattr(wlm_siren_mod, "mute", fake_mute)
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/mute", data={"csrf_token": csrf},
                            headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["muted"] is True
    assert body["ok"] is False
    assert body["error"] == "MQTT connect failed"


# ---------------------------------------------------------------------------
# /siren/unmute — JSON response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unmute_json_clears_state_and_logs(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db
    import wlm.siren as wlm_siren_mod

    db_file, _ = app_env
    unmute_calls = []

    def fake_unmute(conn=None):
        unmute_calls.append(True)

    monkeypatch.setattr(wlm_siren_mod, "unmute", fake_unmute)
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/unmute", data={"csrf_token": csrf},
                            headers={"Accept": "application/json"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["muted"] is False
    assert unmute_calls == [True]

    conn = wlm_db.connect(db_file)
    rows = conn.execute(
        "SELECT message, delivered FROM alerts WHERE kind='siren'"
    ).fetchall()
    conn.close()
    assert any("unmuted from dashboard" in r["message"].lower() for r in rows)


# ---------------------------------------------------------------------------
# Form (redirect) paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mute_form_redirect_success(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    import wlm.siren as wlm_siren_mod

    monkeypatch.setattr(wlm_siren_mod, "mute", lambda conn=None: (True, None))
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/mute", data={"csrf_token": csrf},
                            follow_redirects=True)
    assert resp.status_code == 200
    assert "muted" in resp.text.lower()


@pytest.mark.asyncio
async def test_unmute_form_redirect_success(app_env, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    import wlm.siren as wlm_siren_mod

    monkeypatch.setattr(wlm_siren_mod, "unmute", lambda conn=None: None)
    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        csrf = await _csrf(c)
        resp = await c.post("/siren/unmute", data={"csrf_token": csrf},
                            follow_redirects=True)
    assert resp.status_code == 200
    assert "unmuted" in resp.text.lower()


# ---------------------------------------------------------------------------
# Overview page rendering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_shows_mute_form_when_not_muted(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("mqtt_host", "mqtt.local", conn=conn)
    wlm_db.set_state("siren_muted", "", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.get("/")

    assert "siren-mute-form" in resp.text
    assert "siren-unmute-form" not in resp.text
    assert "SIREN MUTED" not in resp.text


@pytest.mark.asyncio
async def test_overview_shows_unmute_and_badge_when_muted(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_setting("mqtt_host", "mqtt.local", conn=conn)
    wlm_db.set_state("siren_muted", "2099-01-01T00:00:00+00:00", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.get("/")

    assert "siren-unmute-form" in resp.text
    assert "SIREN MUTED" in resp.text
    assert "siren-mute-form" not in resp.text


# ---------------------------------------------------------------------------
# Settings page rendering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_shows_mute_state_not_muted(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_state("siren_muted", "", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.get("/settings")

    assert "NOT MUTED" in resp.text


@pytest.mark.asyncio
async def test_settings_shows_muted_since(app_env):
    from httpx import AsyncClient, ASGITransport
    from wlm import db as wlm_db

    db_file, _ = app_env
    conn = wlm_db.connect(db_file)
    wlm_db.set_state("siren_muted", "2099-06-01T12:00:00+00:00", conn=conn)
    conn.close()

    app = reload_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.cookies.update(await get_session_cookie(c))
        resp = await c.get("/settings")

    # Shows the muted badge and the timestamp
    assert "MUTED" in resp.text
    assert "2099-06-01" in resp.text
