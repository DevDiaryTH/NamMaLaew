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
| `REFERENCE_DESCRIPTION` | *(built-in)* | ✓ | Your site in plain English: what each lens shows, which lens decides CRITICAL, what 0 / 50 / 100 look like |
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

### Rain around the site

Setting `LATITUDE` and `LONGITUDE` (or the equivalent dashboard settings) enables the **RAIN AROUND / ฝนรอบพื้นที่** tile on the overview page.

The tile shows a full-width Leaflet map with an OpenStreetMap base layer and a time selector:

**เรดาร์ · Radar** — animates RainViewer past-radar frames (roughly the last 2 hours). RainViewer free-tier tiles are served at native zoom levels up to z7; Leaflet upscales for higher zooms. No API key required.

**+1 ชม. / +2 ชม. / +3 ชม.** — draws an Open-Meteo model forecast on a 5 × 5 grid across the chosen radius (10, 25, or 50 km). Each grid point is one hourly forecast cell; cells are coloured by hourly intensity band (< 1, 1–2.5, 2.5–7.6, ≥ 7.6 mm/h) and dry cells are left clear. The footer shows the forecast window in local time and credits the model. Buttons for hours not yet returned by the API are disabled automatically.

Forecast data is fetched from `GET /api/rain-forecast?radius=<km>` and cached server-side for 30 minutes per radius. Open-Meteo counts each of the 25 grid points as one API call, so one refresh costs 25 calls (free tier: 10,000 calls per day).

**External requests made by the browser:** `tile.openstreetmap.org` (map tiles), `api.rainviewer.com` + `tilecache.rainviewer.com` (radar tiles). Forecast data is fetched from the local `/api/rain-forecast` endpoint, which in turn queries `api.open-meteo.com`.


## Settings editable from the dashboard (stored in SQLite, override .env)

| Setting key | Default | Description |
|-------------|---------|-------------|
| `telegram_enabled` | `1` | Enable/disable Telegram delivery |
| `telegram_bot_token` | `` | Overrides `.env` value |
| `telegram_chat_id` | `` | |
| `alert_on_warning` | `1` | Alert on entry to warning status |
| `alert_on_recovery` | `1` | Alert on recovery to normal |
| `alert_on_failure` | `1` | Alert after consecutive capture/analysis failures |
| `failure_threshold` | `3` | Consecutive failures before a failure alert fires; also used for a single camera down |
| `critical_repeat_minutes` | `10` | Resend critical alert at most every N minutes |
| `critical_confirm` | `1` | Require a second CRITICAL reading before critical alerts |
| `critical_confirm_recheck_seconds` | `120` | Seconds before re-checking an unconfirmed CRITICAL |
| `heartbeat_hour` | `` | |
| `capture_interval_minutes` | `10` | Minutes between captures |
| `level_warning` | `50` | `level_index` threshold for warning status |
| `level_critical` | `90` | `level_index` threshold for critical status; only used when no red line is drawn |
| `reference_description` | *(built-in)* | Describe your site; see **SET UP FOR YOUR SITE** on the Settings page |
| `claude_model` | `sonnet` | |
| `latitude` | `` | |
| `longitude` | `` | |
| `siren_enabled` | `0` | Sound the MQTT siren on alerts |
| `siren_seconds` | `60` | Seconds the siren sounds before it is switched off |
| `siren_on_warning` | `0` | Also sound the siren when entering WARNING |
| `mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password` / `siren_topic` | see `.env` table | |
| `learning_max_examples` | `3` | Number of verified reference examples sent to Claude per analysis (0 = off, max 6) |
| `learning_use_history` | `1` | Send recent readings (last 2 hours, up to 6) to Claude as context so level_index stays consistent over time. Human-corrected values replace any readings marked wrong. Disable by setting to `0`. |
| `min_confidence` | `0.5` | Force a re-check before a CRITICAL alert when the calibrated confidence is below this value. Set to `0` to disable. Range 0.0–1.0. See [Calibrated confidence](HOW-IT-WORKS.md#calibrated-confidence). |
