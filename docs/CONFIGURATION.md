# Configuration — NamMaLaew

← Back to [README](../README.md)

Settings resolve in this order: dashboard (SQLite) → `.env` → built-in default.

## Environment variables (.env)

| Key | Default | Dashboard editable | Description |
|-----|---------|--------------------|-------------|
| `CAM_STREAMS` | `street=rtsp://host.docker.internal:8554/ec6_2,...` | — | RTSP stream URLs as `label=url,...` |
| `CAM_RTSP_URL` | `` | — | Legacy single-camera URL (used when `CAM_STREAMS` is empty) |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(required)* | — | OAuth token from `claude setup-token` |
| `CLAUDE_MODEL` | `sonnet` | ✓ | Model alias or full id (`sonnet` / `opus` / `haiku`) |
| `TELEGRAM_BOT_TOKEN` | `` | ✓ | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | `` | ✓ | Chat ID for alert delivery |
| `HEARTBEAT_HOUR` | `` | ✓ | Hour (0–23) to send a daily "alive" message; empty = disabled |
| `REFERENCE_DESCRIPTION` | *(built-in)* | ✓ | Scale description sent to Claude for `level_index` calibration |
| `SNAPSHOT_RETENTION_HOURS` | `168` | — | How long to keep snapshots (168 = 7 days) |
| `LATITUDE` | `` | ✓ | Open-Meteo latitude (empty = rainfall data disabled) |
| `LONGITUDE` | `` | ✓ | Open-Meteo longitude |
| `MQTT_HOST` | `` | ✓ | MQTT broker for the siren (empty = siren off) |
| `MQTT_PORT` | `1883` | ✓ | MQTT broker port |
| `MQTT_USERNAME` | `` | ✓ | MQTT login |
| `MQTT_PASSWORD` | `` | ✓ | MQTT password (keep it in `.env`) |
| `SIREN_TOPIC` | `zigbee2mqtt/Siren/set` | ✓ | Topic that receives `{"alarm": true/false}` |
| `DASHBOARD_PASSWORD` | *(required)* | — | Dashboard login password (empty = HTTP 503) |
| `DASHBOARD_SECRET` | *(auto-generated)* | — | Session signing key (auto-generated if empty) |
| `DASHBOARD_PORT` | `8080` | — | Dashboard port |
| `WLM_DB` | `/app/data/wlm.db` | — | SQLite database path |
| `WLM_SNAPSHOT_DIR` | `/app/snapshots` | — | Snapshot storage directory |

## Settings editable from the dashboard (stored in SQLite, override .env)

| Setting key | Default | Description |
|-------------|---------|-------------|
| `telegram_enabled` | `1` | Enable/disable Telegram delivery |
| `telegram_bot_token` | `` | Overrides `.env` value |
| `telegram_chat_id` | `` | |
| `alert_on_warning` | `1` | Alert on entry to warning status |
| `alert_on_recovery` | `1` | Alert on recovery to normal |
| `alert_on_failure` | `1` | Alert after consecutive capture/analysis failures |
| `failure_threshold` | `3` | Consecutive failures before a failure alert fires |
| `critical_repeat_minutes` | `10` | Resend critical alert at most every N minutes |
| `critical_confirm` | `1` | Require a second CRITICAL reading before critical alerts |
| `critical_confirm_recheck_seconds` | `120` | Seconds before re-checking an unconfirmed CRITICAL |
| `heartbeat_hour` | `` | |
| `capture_interval_minutes` | `10` | Minutes between captures |
| `level_warning` | `50` | `level_index` threshold for warning status |
| `level_critical` | `90` | `level_index` threshold for critical status |
| `reference_description` | *(built-in)* | |
| `claude_model` | `sonnet` | |
| `latitude` | `` | |
| `longitude` | `` | |
| `siren_enabled` | `0` | Sound the MQTT siren on alerts |
| `siren_seconds` | `60` | Seconds the siren sounds before it is switched off |
| `siren_on_warning` | `0` | Also sound the siren when entering WARNING |
| `mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password` / `siren_topic` | see `.env` table | |
