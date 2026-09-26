"""Runtime settings: dashboard-editable values stored in SQLite, falling back to env, then defaults.

This module is the shared contract between the monitor (wlm.runner) and the dashboard (web.app).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from wlm import db


@dataclass(frozen=True)
class SettingSpec:
    key: str
    env: str | None
    default: str
    kind: str  # "str" | "int" | "float" | "bool" | "secret" | "text"
    label: str


DEFAULT_REFERENCE_DESCRIPTION = (
    "Describe each lens by its label and what it shows. Say which lens decides critical "
    "(draw red/amber lines on it). level_index scale: 0 = dry; 50 = water at the warning "
    "line; 100 = water at the critical line."
)

SPECS: list[SettingSpec] = [
    SettingSpec("telegram_enabled", None, "1", "bool", "Send Telegram notifications"),
    SettingSpec("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "", "secret", "Telegram bot token"),
    SettingSpec("telegram_chat_id", "TELEGRAM_CHAT_ID", "", "str", "Telegram chat id"),
    SettingSpec("alert_on_warning", None, "1", "bool", "Alert when entering warning"),
    SettingSpec("alert_on_recovery", None, "1", "bool", "Alert when back to normal"),
    SettingSpec("alert_on_failure", None, "1", "bool", "Alert after repeated camera/analysis failures"),
    SettingSpec("failure_threshold", None, "3", "int", "Consecutive failures before failure alert"),
    SettingSpec("critical_repeat_minutes", None, "10", "int", "Repeat critical alert at most every N minutes"),
    SettingSpec("heartbeat_hour", "HEARTBEAT_HOUR", "", "str", "Daily 'alive' message hour (0-23, blank = off)"),
    SettingSpec("capture_interval_minutes", None, "10", "int", "Minutes between snapshots"),
    SettingSpec("level_warning", None, "50", "float", "level_index at or above = warning"),
    SettingSpec("level_critical", None, "90", "float", "level_index at or above = critical"),
    SettingSpec("reference_description", "REFERENCE_DESCRIPTION", DEFAULT_REFERENCE_DESCRIPTION, "text",
                "What normal / warning / critical look like in the frames"),
    SettingSpec("claude_model", "CLAUDE_MODEL", "sonnet", "str",
                "Claude model (CLI alias: sonnet / opus / haiku, or full model id)"),
    SettingSpec("latitude", "LATITUDE", "", "str", "Latitude for rainfall (blank = off)"),
    SettingSpec("longitude", "LONGITUDE", "", "str", "Longitude for rainfall (blank = off)"),
    # --- Siren (MQTT) ---
    SettingSpec("siren_enabled", None, "0", "bool", "Sound the MQTT siren on alerts"),
    SettingSpec("mqtt_host", "MQTT_HOST", "", "str", "MQTT broker hostname or IP"),
    SettingSpec("mqtt_port", "MQTT_PORT", "1883", "int", "MQTT broker port"),
    SettingSpec("mqtt_username", "MQTT_USERNAME", "", "str", "MQTT username"),
    SettingSpec("mqtt_password", "MQTT_PASSWORD", "", "secret", "MQTT password"),
    SettingSpec("siren_topic", "SIREN_TOPIC", "zigbee2mqtt/Siren/set", "str", "MQTT topic for the siren"),
    SettingSpec("siren_seconds", None, "60", "int", "Seconds the siren sounds before it is switched off"),
    SettingSpec("siren_on_warning", None, "0", "bool", "Also sound the siren when entering WARNING"),
    # --- Critical confirmation ---
    SettingSpec("critical_confirm", None, "1", "bool",
                "Require a second CRITICAL reading before critical alerts"),
    SettingSpec("critical_confirm_recheck_seconds", None, "120", "int",
                "Seconds before re-checking an unconfirmed CRITICAL"),
]

SPEC_BY_KEY = {s.key: s for s in SPECS}


def get(key: str, conn=None) -> str:
    """DB value if set and non-empty, else env var (if any) if non-empty, else default."""
    spec = SPEC_BY_KEY[key]
    value = db.get_setting(key, conn=conn)
    if value not in (None, ""):
        return value
    if spec.env:
        env_value = os.getenv(spec.env, "")
        if env_value != "":
            return env_value
    return spec.default


def get_bool(key: str, conn=None) -> bool:
    return get(key, conn).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, conn=None) -> int:
    try:
        return int(float(get(key, conn)))
    except ValueError:
        return int(float(SPEC_BY_KEY[key].default))


def get_float(key: str, conn=None) -> float:
    try:
        return float(get(key, conn))
    except ValueError:
        return float(SPEC_BY_KEY[key].default)


def all_settings(conn=None) -> dict[str, str]:
    return {s.key: get(s.key, conn) for s in SPECS}
