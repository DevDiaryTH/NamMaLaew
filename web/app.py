"""FastAPI dashboard for NamMaLaew (water-level monitor).

Runs via: uvicorn web.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from wlm import db as wlm_db
from wlm import settings as wlm_settings
from wlm import lines as wlm_lines
from wlm.capture import parse_cam_streams
from web import metrics as web_metrics
from wlm.rain_forecast import get_rain_forecast, ALLOWED_RADII

logger = logging.getLogger("web.app")

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="NamMaLaew / น้ำมาแล้ว! Dashboard")

# Mount static files (no auth needed for CSS/JS/vendor)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _get_or_create_secret() -> str:
    """Return DASHBOARD_SECRET env var; if empty, load or generate one from runtime_state."""
    env_secret = os.getenv("DASHBOARD_SECRET", "")
    if env_secret:
        return env_secret
    try:
        conn = wlm_db.connect()
        stored = wlm_db.get_state("dashboard_secret", conn=conn)
        if stored:
            conn.close()
            return stored
        new_secret = secrets.token_hex(32)
        wlm_db.set_state("dashboard_secret", new_secret, conn=conn)
        conn.close()
        return new_secret
    except Exception:
        return secrets.token_hex(32)


app.add_middleware(
    SessionMiddleware,
    secret_key=_get_or_create_secret(),
    session_cookie="wlm_session",
    https_only=False,
    same_site="lax",
    max_age=86400 * 7,
)

# ---------------------------------------------------------------------------
# Brute-force tracking
# ---------------------------------------------------------------------------
_ip_failures: dict[str, list[float]] = defaultdict(list)
_IP_WINDOW = 300  # 5 minutes
_IP_MAX = 10


def _record_failure(ip: str) -> None:
    now = time.time()
    _ip_failures[ip] = [t for t in _ip_failures[ip] if now - t < _IP_WINDOW]
    _ip_failures[ip].append(now)


def _is_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _ip_failures[ip] if now - t < _IP_WINDOW]
    _ip_failures[ip] = recent
    return len(recent) >= _IP_MAX


def _get_client_ip(request: Request) -> str:
    # No reverse proxy in front of this app, so X-Forwarded-For is client-controlled; ignore it.
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "")


def _is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _generate_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        request.session["csrf_token"] = token
    return token


def _check_csrf(request: Request, csrf_token: str) -> bool:
    expected = request.session.get("csrf_token", "")
    return hmac.compare_digest(expected, csrf_token or "")


# ---------------------------------------------------------------------------
# Template helpers / context
# ---------------------------------------------------------------------------

def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "csrf_token": _generate_csrf(request),
    }


def _no_password_response(request: Request) -> HTMLResponse:
    """Show a clear setup page when DASHBOARD_PASSWORD is not configured."""
    ctx = _base_context(request)
    return templates.TemplateResponse(request, "setup.html", ctx, status_code=503)


def _redirect_login(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# --- Health check (no auth) ---

@app.get("/healthz")
async def healthz():
    db_ok = False
    try:
        conn = wlm_db.connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass
    return {"ok": True, "db": db_ok}


# --- Login ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    ctx = _base_context(request)
    ctx["error"] = None
    return templates.TemplateResponse(request, "login.html", ctx)


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)

    ip = _get_client_ip(request)
    if _is_blocked(ip):
        ctx = _base_context(request)
        ctx["error"] = "Too many failed attempts. Please wait."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=429)

    if not _check_csrf(request, csrf_token):
        ctx = _base_context(request)
        ctx["error"] = "Invalid CSRF token."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=400)

    await asyncio.sleep(0)  # yield before compare
    if not hmac.compare_digest(pwd.encode("utf-8"), password.encode("utf-8")):
        await asyncio.sleep(1)
        _record_failure(ip)
        ctx = _base_context(request)
        ctx["error"] = "Incorrect password."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=401)

    request.session["authenticated"] = True
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# --- Auth guard dependency ---

def require_auth(request: Request):
    pwd = _get_password()
    if not pwd:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD not set")
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


# --- Overview page (/) ---

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        summary = web_metrics.get_summary(conn)
        health = web_metrics.get_system_health(conn)
        costs = web_metrics.get_cost_metrics(conn)
        capture_interval = wlm_settings.get_int("capture_interval_minutes", conn=conn)
        level_critical = wlm_settings.get_float("level_critical", conn=conn)
        level_warning = wlm_settings.get_float("level_warning", conn=conn)

        stale = web_metrics.is_stale(conn, capture_interval)
        slope_data = web_metrics.compute_rise_rate(conn)
        slope = slope_data["slope"] if slope_data else None
        slope_r2 = slope_data["r2"] if slope_data else None
        eta = web_metrics.compute_eta_to_critical(conn, level_critical, slope=slope_data)
        # Lets the template tell "not enough data" (<3 points) from "not rising".
        rise_rate_pts = web_metrics.count_window_points(conn)
        siren_host = bool(wlm_settings.get("mqtt_host", conn=conn))
        siren_muted = bool(wlm_db.get_state("siren_muted", default="", conn=conn))
        try:
            site_lat = float(wlm_settings.get("latitude",  conn=conn).strip())
            site_lon = float(wlm_settings.get("longitude", conn=conn).strip())
        except (ValueError, AttributeError):
            site_lat = None
            site_lon = None
        conn.close()
    except Exception as e:
        logger.exception("Overview DB error")
        summary = {"status": "unknown", "level_index": None, "ts": None, "stale": True, "lenses": []}
        health = {}
        costs = {}
        stale = True
        slope = None
        slope_r2 = None
        eta = None
        rise_rate_pts = 0
        siren_host = False
        siren_muted = False
        level_critical = 90.0
        level_warning = 50.0
        site_lat = None
        site_lon = None

    ctx = _base_context(request)
    ctx.update({
        "summary": summary,
        "health": health,
        "costs": costs,
        "stale": stale,
        "slope": slope,
        "slope_r2": slope_r2,
        "eta": eta,
        "rise_rate_pts": rise_rate_pts,
        "siren_host": siren_host,
        "siren_muted": siren_muted,
        "level_critical": level_critical,
        "level_warning": level_warning,
        "site_lat": site_lat,
        "site_lon": site_lon,
    })
    return templates.TemplateResponse(request, "index.html", ctx)


# --- Timeline page ---

@app.get("/timeline", response_class=HTMLResponse)
async def timeline(
    request: Request,
    page: int = 1,
    status: str = "",
    range: str = "",
):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        result = web_metrics.get_readings_page(
            conn,
            page=page,
            per_page=50,
            status_filter=status or None,
            range_str=range or None,
        )
        conn.close()
    except Exception:
        logger.exception("Timeline DB error")
        result = {"readings": [], "total": 0, "page": 1, "pages": 1, "per_page": 50}

    ctx = _base_context(request)
    ctx.update(result)
    ctx["status_filter"] = status
    ctx["range_filter"] = range
    return templates.TemplateResponse(request, "timeline.html", ctx)


# --- Reading detail ---

@app.get("/reading/{reading_id}", response_class=HTMLResponse)
async def reading_detail(request: Request, reading_id: int):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        reading = web_metrics.get_reading(conn, reading_id)
        conn.close()
    except Exception:
        logger.exception("Reading detail DB error")
        reading = None

    if reading is None:
        raise HTTPException(status_code=404, detail="Reading not found")

    ctx = _base_context(request)
    ctx["reading"] = reading
    return templates.TemplateResponse(request, "reading_detail.html", ctx)


# --- Alerts page ---

@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, page: int = 1):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        result = web_metrics.get_alerts(conn, page=page)
        conn.close()
    except Exception:
        logger.exception("Alerts DB error")
        result = {"alerts": [], "total": 0, "page": 1, "pages": 1, "per_page": 50}

    ctx = _base_context(request)
    ctx.update(result)
    return templates.TemplateResponse(request, "alerts.html", ctx)


# --- Settings page ---

def _sources_for_settings(conn) -> dict[str, str]:
    """Return source label for each setting key: 'db', 'env', or 'default'."""
    sources = {}
    for spec in wlm_settings.SPECS:
        db_val = wlm_db.get_setting(spec.key, conn=conn)
        if db_val not in (None, ""):
            sources[spec.key] = "db"
        elif spec.env and os.getenv(spec.env, "") != "":
            sources[spec.key] = "env"
        else:
            sources[spec.key] = "default"
    return sources


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        current = wlm_settings.all_settings(conn=conn)
        sources = _sources_for_settings(conn)
        siren_muted_since = wlm_db.get_state("siren_muted", default="", conn=conn) or ""
        setup = _site_setup_status(conn)
        conn.close()
    except Exception:
        logger.exception("Settings DB error")
        current = {s.key: s.default for s in wlm_settings.SPECS}
        sources = {s.key: "default" for s in wlm_settings.SPECS}
        siren_muted_since = ""
        setup = None

    ctx = _base_context(request)
    ctx.update({
        "specs": wlm_settings.SPECS,
        "current": current,
        "sources": sources,
        "siren_muted_since": siren_muted_since,
        "setup": setup,
        "flash": request.session.pop("flash", None),
        "flash_error": request.session.pop("flash_error", None),
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


def _site_setup_status(conn) -> dict:
    """Status chips for the Settings page's "Set up for your site" checklist."""
    lenses = [label for label, _url in parse_cam_streams()]
    deciding = sorted(
        label for label, kinds in wlm_lines.get_lines(conn=conn).items() if "critical" in kinds
    )
    customised = (
        wlm_settings.get("reference_description", conn=conn)
        != wlm_settings.DEFAULT_REFERENCE_DESCRIPTION
    )
    steps = {
        "cameras": (bool(lenses),
                    f"{len(lenses)} LENS{'ES' if len(lenses) != 1 else ''}: {', '.join(lenses)}"
                    if lenses else "NO CAMERAS SET"),
        "lines": (bool(deciding),
                  f"RED LINE ON {', '.join(deciding)}" if deciding else "NO RED LINE YET"),
        "description": (customised, "CUSTOMISED" if customised else "STILL THE DEFAULT"),
    }
    done = sum(ok for ok, _text in steps.values())
    return {
        "steps": {key: {"ok": ok, "text": text} for key, (ok, text) in steps.items()},
        "done": done,
        "total": len(steps),
    }


def _validate_settings(form_data: dict) -> list[str]:
    """Return list of validation error strings."""
    errors = []
    try:
        lw = float(form_data.get("level_warning", 50))
        lc = float(form_data.get("level_critical", 90))
        if lw >= lc:
            errors.append("level_warning must be less than level_critical")
    except (ValueError, TypeError):
        errors.append("level_warning and level_critical must be numbers")

    try:
        ci = int(form_data.get("capture_interval_minutes", 10))
        if not (1 <= ci <= 120):
            errors.append("capture_interval_minutes must be 1-120")
    except (ValueError, TypeError):
        errors.append("capture_interval_minutes must be an integer")

    hh = form_data.get("heartbeat_hour", "").strip()
    if hh:
        try:
            h = int(hh)
            if not (0 <= h <= 23):
                errors.append("heartbeat_hour must be 0-23 or blank")
        except (ValueError, TypeError):
            errors.append("heartbeat_hour must be an integer 0-23 or blank")

    lat = form_data.get("latitude", "").strip()
    if lat:
        try:
            lv = float(lat)
            if not (-90 <= lv <= 90):
                errors.append("latitude must be -90 to 90")
        except (ValueError, TypeError):
            errors.append("latitude must be a number")

    lon = form_data.get("longitude", "").strip()
    if lon:
        try:
            lv = float(lon)
            if not (-180 <= lv <= 180):
                errors.append("longitude must be -180 to 180")
        except (ValueError, TypeError):
            errors.append("longitude must be a number")

    return errors


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    form = await request.form()
    form_data = dict(form)

    if not _check_csrf(request, form_data.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    errors = _validate_settings(form_data)
    if errors:
        try:
            conn = wlm_db.connect()
            current = wlm_settings.all_settings(conn=conn)
            sources = _sources_for_settings(conn)
            setup = _site_setup_status(conn)
            conn.close()
        except Exception:
            current = {s.key: s.default for s in wlm_settings.SPECS}
            sources = {s.key: "default" for s in wlm_settings.SPECS}
            setup = None
        ctx = _base_context(request)
        ctx.update({
            "specs": wlm_settings.SPECS,
            "current": current,
            "sources": sources,
            "setup": setup,
            "flash": None,
            "flash_error": "; ".join(errors),
        })
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=422)

    try:
        conn = wlm_db.connect()
        for spec in wlm_settings.SPECS:
            key = spec.key
            if spec.kind == "bool":
                val = "1" if form_data.get(key) in ("1", "on", "true") else "0"
                wlm_db.set_setting(key, val, conn=conn)
            elif spec.kind == "secret":
                # Blank → keep existing; explicit clear checkbox → delete
                clear = form_data.get(f"{key}_clear") == "1"
                raw = form_data.get(key, "").strip()
                if clear:
                    wlm_db.set_setting(key, "", conn=conn)
                elif raw:
                    wlm_db.set_setting(key, raw, conn=conn)
                # else: do nothing (keep existing)
            else:
                val = form_data.get(key, "").strip()
                wlm_db.set_setting(key, val, conn=conn)
        conn.close()
    except Exception as e:
        logger.exception("Settings save error")
        request.session["flash_error"] = f"Save failed: {e}"
        return RedirectResponse(url="/settings", status_code=302)

    request.session["flash"] = "Settings saved."
    return RedirectResponse(url="/settings", status_code=302)


@app.post("/settings/test-telegram", response_class=HTMLResponse)
async def test_telegram(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    form = await request.form()
    if not _check_csrf(request, form.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    try:
        conn = wlm_db.connect()
        token = wlm_settings.get("telegram_bot_token", conn=conn)
        chat_id = wlm_settings.get("telegram_chat_id", conn=conn)
        conn.close()
    except Exception as e:
        request.session["flash_error"] = f"Could not read settings: {e}"
        return RedirectResponse(url="/settings", status_code=302)

    if not token or not chat_id:
        request.session["flash_error"] = "Telegram bot token and chat ID must be set first."
        return RedirectResponse(url="/settings", status_code=302)

    try:
        import sys
        # Lazy import so tests can inject a mock
        if "wlm.telegram" not in sys.modules:
            from wlm import telegram as _tg_mod  # type: ignore
            sys.modules["wlm.telegram"] = _tg_mod
        tg = sys.modules["wlm.telegram"]
        ok, err = tg.send_message(token, chat_id, "NamMaLaew / น้ำมาแล้ว!: test message from dashboard.")
    except ImportError:
        request.session["flash_error"] = "wlm.telegram not available."
        return RedirectResponse(url="/settings", status_code=302)
    except Exception as e:
        ok, err = False, str(e)

    try:
        conn = wlm_db.connect()
        wlm_db.insert_alert(
            kind="test",
            message="Dashboard test message",
            delivered=ok,
            error=err,
            conn=conn,
        )
        conn.close()
    except Exception:
        pass

    if ok:
        request.session["flash"] = "Test Telegram message sent successfully."
    else:
        request.session["flash_error"] = f"Telegram test failed: {err}"
    return RedirectResponse(url="/settings", status_code=302)


@app.post("/settings/test-siren", response_class=HTMLResponse)
async def test_siren(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    form = await request.form()
    if not _check_csrf(request, form.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    from wlm import siren as wlm_siren

    # Always send OFF after an ON attempt: an ON that reports failure may still
    # have reached the siren, and it has no timer of its own.
    try:
        ok_on, err_on = await asyncio.to_thread(wlm_siren.publish, {"alarm": True}, None, True)
    except Exception as exc:
        ok_on, err_on = False, str(exc)

    await asyncio.sleep(1)

    try:
        ok_off, err_off = await asyncio.to_thread(wlm_siren.publish, {"alarm": False}, None, True)
    except Exception as exc:
        ok_off, err_off = False, str(exc)

    if not ok_on:
        request.session["flash_error"] = f"Siren test failed (ON): {err_on}"
        return RedirectResponse(url="/settings", status_code=302)

    try:
        conn = wlm_db.connect()
        wlm_db.insert_alert(
            kind="siren",
            message="Dashboard test siren (1 s)",
            delivered=ok_off,
            error=err_off,
            conn=conn,
        )
        conn.close()
    except Exception:
        pass

    if ok_off:
        request.session["flash"] = "Siren test completed (1 s)."
    else:
        request.session["flash_error"] = f"Siren test failed (OFF): {err_off}"
    return RedirectResponse(url="/settings", status_code=302)


# --- Stop-siren button ---

@app.post("/siren/stop")
async def stop_siren(request: Request):
    """Silence the siren now. JSON for the overview button, redirect for the Settings form."""
    pwd = _get_password()
    wants_json = "application/json" in request.headers.get("accept", "")
    if not pwd:
        raise HTTPException(status_code=503)
    if not _is_authenticated(request):
        if wants_json:
            raise HTTPException(status_code=401)
        return _redirect_login(request)

    form = await request.form()
    if not _check_csrf(request, form.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    from wlm import siren as wlm_siren

    try:
        ok, err = await asyncio.to_thread(wlm_siren.stop, None, True)
    except Exception as exc:
        ok, err = False, str(exc)

    try:
        conn = wlm_db.connect()
        wlm_db.insert_alert(kind="siren", message="Siren stopped from dashboard",
                            delivered=ok, error=err, conn=conn)
        conn.close()
    except Exception:
        pass

    if wants_json:
        return {"ok": ok, "error": err}
    if ok:
        request.session["flash"] = "Siren stopped."
    else:
        request.session["flash_error"] = f"Siren stop failed: {err}"
    return RedirectResponse(url="/settings", status_code=302)


# --- Mute-siren button ---

@app.post("/siren/mute")
async def mute_siren(request: Request):
    """Mute the siren until the water drops below its current level. JSON for the overview button."""
    pwd = _get_password()
    wants_json = "application/json" in request.headers.get("accept", "")
    if not pwd:
        raise HTTPException(status_code=503)
    if not _is_authenticated(request):
        if wants_json:
            raise HTTPException(status_code=401)
        return _redirect_login(request)

    form = await request.form()
    if not _check_csrf(request, form.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    from wlm import siren as wlm_siren

    try:
        ok, err = await asyncio.to_thread(wlm_siren.mute, None)
    except Exception as exc:
        ok, err = False, str(exc)

    try:
        conn = wlm_db.connect()
        wlm_db.insert_alert(
            kind="siren",
            message="Siren muted from dashboard until the water drops",
            delivered=ok, error=err, conn=conn,
        )
        conn.close()
    except Exception:
        pass

    if wants_json:
        # muted=True always: the state is set even when the stop publish fails.
        return {"ok": ok, "error": err, "muted": True}
    if ok:
        request.session["flash"] = "Siren muted until the water drops."
    else:
        request.session["flash_error"] = f"Siren mute (stop publish failed): {err}"
    return RedirectResponse(url="/settings", status_code=302)


# --- Unmute-siren button ---

@app.post("/siren/unmute")
async def unmute_siren(request: Request):
    """Manually unmute the siren. JSON for the overview button."""
    pwd = _get_password()
    wants_json = "application/json" in request.headers.get("accept", "")
    if not pwd:
        raise HTTPException(status_code=503)
    if not _is_authenticated(request):
        if wants_json:
            raise HTTPException(status_code=401)
        return _redirect_login(request)

    form = await request.form()
    if not _check_csrf(request, form.get("csrf_token", "")):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    from wlm import siren as wlm_siren

    try:
        await asyncio.to_thread(wlm_siren.unmute, None)
    except Exception:
        pass

    try:
        conn = wlm_db.connect()
        wlm_db.insert_alert(
            kind="siren",
            message="Siren unmuted from dashboard",
            delivered=True, error=None, conn=conn,
        )
        conn.close()
    except Exception:
        pass

    if wants_json:
        return {"ok": True, "muted": False}
    request.session["flash"] = "Siren unmuted."
    return RedirectResponse(url="/settings", status_code=302)


# --- Run-now button ---

@app.post("/run-now")
async def run_now(request: Request, csrf_token: str = Form("")):
    pwd = _get_password()
    if not pwd:
        raise HTTPException(status_code=503)
    if not _is_authenticated(request):
        raise HTTPException(status_code=401)
    if not _check_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    try:
        conn = wlm_db.connect()
        wlm_db.set_state("run_now_requested", wlm_db.now_iso(), conn=conn)
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True}


# --- Snapshot file serving (auth required, path-traversal protected) ---

@app.get("/snapshots/{path:path}")
async def serve_snapshot(request: Request, path: str):
    pwd = _get_password()
    if not pwd:
        raise HTTPException(status_code=503)
    if not _is_authenticated(request):
        raise HTTPException(status_code=401)

    snap_dir = wlm_db.snapshot_dir().resolve()

    # Reject obvious traversal attempts before resolve
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    target = (snap_dir / path).resolve()

    # After resolve, ensure the target is inside snap_dir
    try:
        target.relative_to(snap_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal denied")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return FileResponse(str(target))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/api/summary")
async def api_summary(request: Request, _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        summary = web_metrics.get_summary(conn)
        capture_interval = wlm_settings.get_int("capture_interval_minutes", conn=conn)
        stale = web_metrics.is_stale(conn, capture_interval)
        level_critical = wlm_settings.get_float("level_critical", conn=conn)
        level_warning = wlm_settings.get_float("level_warning", conn=conn)
        slope_data = web_metrics.compute_rise_rate(conn)
        slope = slope_data["slope"] if slope_data else None
        slope_r2 = slope_data["r2"] if slope_data else None
        eta = web_metrics.compute_eta_to_critical(conn, level_critical, slope=slope_data)
        health = web_metrics.get_system_health(conn)
        conn.close()
    except Exception as e:
        logger.exception("API summary error")
        raise HTTPException(status_code=500, detail=str(e))

    # Serialize eta datetimes
    if eta and eta.get("eta_dt"):
        eta = {**eta, "eta_dt": eta["eta_dt"].isoformat()}

    return {
        "summary": summary,
        "stale": stale,
        "slope": slope,
        "slope_r2": slope_r2,
        "eta": eta,
        "level_critical": level_critical,
        "level_warning": level_warning,
        "health": health,
    }


@app.get("/api/series")
async def api_series(request: Request, range: str = "24h", _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        data = web_metrics.get_series(conn, range)
        conn.close()
    except Exception as e:
        logger.exception("API series error")
        raise HTTPException(status_code=500, detail=str(e))
    return data


@app.get("/api/rain-forecast")
def api_rain_forecast(request: Request, radius: int = 25, _=Depends(require_auth)):
    # Plain def: FastAPI runs it in a worker thread, so the blocking HTTP call
    # to Open-Meteo doesn't stall the event loop.
    if radius not in ALLOWED_RADII:
        raise HTTPException(status_code=400, detail=f"radius must be one of {list(ALLOWED_RADII)}")
    try:
        conn = wlm_db.connect()
        data = get_rain_forecast(radius_km=radius, conn=conn)
        conn.close()
    except Exception as exc:
        logger.warning("rain-forecast fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="rain data unavailable")
    if data is None:
        return {"enabled": False}
    return {"enabled": True, **data}


@app.get("/api/readings")
async def api_readings(
    request: Request,
    page: int = 1,
    status: str = "",
    range: str = "",
    _=Depends(require_auth),
):
    try:
        conn = wlm_db.connect()
        result = web_metrics.get_readings_page(
            conn,
            page=page,
            status_filter=status or None,
            range_str=range or None,
        )
        conn.close()
    except Exception as e:
        logger.exception("API readings error")
        raise HTTPException(status_code=500, detail=str(e))
    return result


@app.get("/api/alerts")
async def api_alerts(request: Request, page: int = 1, _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        result = web_metrics.get_alerts(conn, page=page)
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@app.get("/api/costs")
async def api_costs(request: Request, _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        result = web_metrics.get_cost_metrics(conn)
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


@app.get("/api/image-series")
async def api_image_series(request: Request, range: str = "24h", _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        data = web_metrics.get_image_series(conn, range)
        conn.close()
    except Exception as e:
        logger.exception("API image-series error")
        raise HTTPException(status_code=500, detail=str(e))
    return data


# ---------------------------------------------------------------------------
# Alert lines API
# ---------------------------------------------------------------------------

@app.get("/api/lines")
async def api_lines_get(request: Request, _=Depends(require_auth)):
    try:
        conn = wlm_db.connect()
        data = wlm_lines.get_lines(conn=conn)
        conn.close()
    except Exception as e:
        logger.exception("API lines GET error")
        raise HTTPException(status_code=500, detail=str(e))
    return data


@app.post("/api/lines")
async def api_lines_post(request: Request, _=Depends(require_auth)):
    # CSRF token in request header
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not _check_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        conn = wlm_db.connect()
        cleaned = wlm_lines.set_lines(body, conn=conn)
        conn.close()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("API lines POST error")
        raise HTTPException(status_code=500, detail=str(e))

    return cleaned


# ---------------------------------------------------------------------------
# Alert lines editor page
# ---------------------------------------------------------------------------

def _get_lens_labels(conn) -> list[str]:
    """Return distinct lens labels from recent readings; fallback to CAM_STREAMS env."""
    rows = conn.execute(
        "SELECT DISTINCT label FROM lens_readings ORDER BY label"
    ).fetchall()
    if rows:
        return [r["label"] for r in rows]
    # Fallback: parse CAM_STREAMS="label=url,label2=url2"
    cam_streams = os.getenv("CAM_STREAMS", "")
    labels = []
    for part in cam_streams.split(","):
        part = part.strip()
        if "=" in part:
            labels.append(part.split("=", 1)[0].strip())
    return labels or ["street", "carport"]


def _get_latest_snapshot_per_lens(conn) -> dict[str, str | None]:
    """Return {label: snapshot_path} for the latest successful capture per lens."""
    rows = conn.execute(
        """SELECT lr.label, lr.snapshot_path
           FROM lens_readings lr
           JOIN readings r ON r.id = lr.reading_id
           WHERE lr.snapshot_path IS NOT NULL AND lr.ok = 1
           ORDER BY r.ts DESC"""
    ).fetchall()
    result: dict[str, str | None] = {}
    for row in rows:
        label = row["label"]
        if label not in result:
            result[label] = row["snapshot_path"]
    return result


@app.get("/lines", response_class=HTMLResponse)
async def lines_page(request: Request):
    pwd = _get_password()
    if not pwd:
        return _no_password_response(request)
    if not _is_authenticated(request):
        return _redirect_login(request)

    try:
        conn = wlm_db.connect()
        labels = _get_lens_labels(conn)
        snapshots = _get_latest_snapshot_per_lens(conn)
        current_lines = wlm_lines.get_lines(conn=conn)
        # Get last-saved time for level_lines setting
        row = conn.execute(
            "SELECT updated_at FROM settings WHERE key = ?", ("level_lines",)
        ).fetchone()
        last_saved = row["updated_at"] if row else None
        conn.close()
    except Exception:
        logger.exception("Lines page DB error")
        labels = []
        snapshots = {}
        current_lines = {}
        last_saved = None

    ctx = _base_context(request)
    ctx.update({
        "labels": labels,
        "snapshots": snapshots,
        "current_lines": current_lines,
        "last_saved": last_saved,
    })
    return templates.TemplateResponse(request, "lines.html", ctx)
