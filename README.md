# น้ำมาแล้ว! — NamMaLaew

> A CCTV camera + AI that wakes you before the flood reaches your door.

Captures snapshots from both camera lenses every N minutes, analyzes them with `claude -p` to produce a `level_status` (`normal` / `warning` / `critical` / `unknown`) and a 0–100 `level_index`, and fires a Telegram alert the moment the level crosses a line you drew on your own camera image. Built for the IMILAB EC6 Dual (Xiaomi model `chuangmi.camera.068ac1`) — a dual-lens camera with no RTSP, no HTTP, and no ONVIF.

<p align="center">
  <img src="docs/dashboard.png" width="100%" alt="NamMaLaew dashboard: current water level, rise rate, system health, cost, water-level and image-quality charts, and the latest snapshots from both lenses">
</p>

---

## Quick Start

These 5 steps link to the detailed install sections. Total time: ~40 min.

1. **Clone and get your camera IP** → [Step 1](#step-1--clone-repository), [Step 2](#step-2--find-the-camera-ip) (~5 min)
2. **Install go2rtc and log in to Xiaomi** → [Steps 3–7](#step-3--install-go2rtc) (~15 min incl. Xiaomi login)
3. **Get a Claude OAuth token and Telegram bot** → [Steps 9–10](#step-9--get-a-claude-code-oauth-token) (~7 min)
4. **Fill `.env`, build, and open the dashboard** → [Steps 11–13](#step-11--configure-env) (~5 min + ~5–10 min first Docker build)
5. **Draw your water-level alert lines** → [Step 14](#step-14--draw-alert-lines) (~3 min)

---

## Why This Exists

I live in Thailand. In the rainy season, street flooding can reach the house and the carport overnight. I wanted to sleep without getting up to check the street.

I already had an IMILAB EC6 Dual camera pointed at the street and carport. The first problem: the camera speaks only Xiaomi's cloud and P2P protocol. No RTSP, no HTTP, no ONVIF. Every standard connection attempt failed. The fix was go2rtc's Xiaomi source, which logs in to Xiaomi's cloud once, then pulls the stream from the camera on the LAN and re-serves it as RTSP on localhost.

From there the logic is simple. Claude looks at both lenses every 10 minutes and returns a 0–100 water-level index. Telegram wakes me only when the level crosses a line I drew myself on the real camera image. The whole thing runs on a Mac at home with Docker and a Claude subscription; no separate API key needed.

If you have a Xiaomi camera and live somewhere that floods, this project lets you sleep through the rainy season.

---

## Architecture

```
Mac Host
├── go2rtc  (native — not inside Docker)
│     ├── rtsp://127.0.0.1:8554/ec6      ← lens: carport
│     └── rtsp://127.0.0.1:8554/ec6_2   ← lens: street
│
└── Docker Compose
      ├── monitor    python -m wlm.runner --loop
      │               capture → overlay → claude -p → alerts → DB
      └── dashboard  uvicorn web.app:app  (port 8080)

volumes: ./data  ./snapshots  ./logs
```

**Why go2rtc runs on the Mac directly (not in Docker)**

The IMILAB EC6 Dual has no RTSP, HTTP, or ONVIF — it uses Xiaomi's miio/P2P protocol over UDP port 54321. go2rtc v1.9.14+ has a Xiaomi source that logs in to Xiaomi cloud once, then pulls the stream from the camera on the LAN and re-serves it as RTSP on `127.0.0.1:8554`. When go2rtc runs on the Mac directly, Docker containers reach it via `host.docker.internal:8554`.

**Note:** go2rtc launched via launchd (or inside Docker) fails to connect to the camera on some machines. Suspected cause: macOS Local Network privacy permission. See [Known Issues](#known-issues).

---

## Requirements

**Must have**

| Item | Notes |
|------|-------|
| macOS (Apple Silicon or Intel) | Tested on Apple Silicon |
| [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/) | Set to "Start at Login" |
| go2rtc v1.9.14+ | Downloaded manually — see install steps below |
| Claude subscription + Claude Code CLI | `claude` binary in container comes from npm; OAuth token from Mac |
| Mi Home account + IMILAB EC6 Dual camera | Camera must be registered in Mi Home first |
| Telegram account | Create a bot via @BotFather |

**Optional**

| Item | Notes |
|------|-------|
| ffmpeg (on Mac) | Useful for manually testing RTSP streams; not required if you only use Docker |

---

## Installation

### Step 1 — Clone repository

~2 min

```bash
git clone <repo-url> NamMaLaew
cd NamMaLaew
```

✅ Works when: `ls compose.yaml wlm/runner.py` prints both files without error.

---

### Step 2 — Find the camera IP

~3 min

**Recommended:** Open **Mi Home** → camera → Settings → "Network info" / "About" — shows IP and MAC address.

**Alternatives:**
```bash
# Your Mac's IP
ipconfig getifaddr en0

# LAN device list
arp -a
```

**Probe the camera** (expects a reply within 1 second):
```bash
python3 -c "
import socket, struct, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(1)
s.sendto(bytes.fromhex('21310020' + 'ff'*28), ('<CAMERA_IP>', 54321))
try:
    data, _ = s.recvfrom(64)
    print('OK, got', len(data), 'bytes:', data.hex())
except Exception as e:
    print('No response:', e)
"
```

A 32-byte reply confirms the camera is at that IP. **Set a DHCP reservation on your router** so the IP does not change.

> **Warning:** Other LAN devices (e.g., cameras running LIVE555) may already answer RTSP on port 8554 — do not confuse them with go2rtc.

✅ Works when: The probe returns `OK, got 32 bytes:` followed by a hex string.

---

### Step 3 — Install go2rtc

~2 min

**Apple Silicon (arm64):**
```bash
mkdir -p go2rtc
curl -L https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_mac_arm64.zip -o /tmp/go2rtc.zip
unzip /tmp/go2rtc.zip -d go2rtc/
xattr -d com.apple.quarantine go2rtc/go2rtc
```

**Intel (amd64):**
```bash
mkdir -p go2rtc
curl -L https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_mac_amd64.zip -o /tmp/go2rtc.zip
unzip /tmp/go2rtc.zip -d go2rtc/
xattr -d com.apple.quarantine go2rtc/go2rtc
```

✅ Works when: `go2rtc/go2rtc --version` prints `go2rtc version v1.9.x`.

---

### Step 4 — Create go2rtc config from template

~1 min

```bash
cp go2rtc/go2rtc.yaml.example go2rtc/go2rtc.yaml
```

The `go2rtc.yaml.example` template contains only the API and RTSP listen settings:
```yaml
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: "127.0.0.1:8554"
```

After Xiaomi login (Step 5), go2rtc automatically appends a `xiaomi:` section with the token to this file.

✅ Works when: `grep 'listen: "127.0.0.1:1984"' go2rtc/go2rtc.yaml` returns a match.

---

### Step 5 — Start go2rtc and log in to Xiaomi

~5 min (includes Xiaomi login + possible captcha/2FA)

```bash
cd go2rtc && ./go2rtc -config go2rtc.yaml
```

(First run has no `streams.yaml` yet — that is expected. The `-config streams.yaml` flag is added in Step 7.)

Open a browser at **http://127.0.0.1:1984** → click **"Add"** → select **"Xiaomi"** → enter your Mi Home account credentials (may require captcha or 2FA code) → click login.

go2rtc saves the token into `go2rtc/go2rtc.yaml`. This file is gitignored — **do not commit it**.

✅ Works when: `grep -q 'xiaomi:' go2rtc/go2rtc.yaml && echo "token saved"` prints `token saved`.

---

### Step 6 — Find the Device DID and region

~2 min

After login, list your devices. Replace `<XIAOMI_USER_ID>` with your Mi Home User ID (Mi Home app → profile → User ID).

```bash
curl "http://127.0.0.1:1984/api/xiaomi?id=<XIAOMI_USER_ID>&region=<REGION>"
```

Regions to try: `sg` (Southeast Asia), `cn`, `de`, `i2`, `ru`, `us`.

The response lists devices with their `did` values. Find the `did` for model `chuangmi.camera.068ac1`.

✅ Works when: The JSON response includes a device with `"model": "chuangmi.camera.068ac1"`.

---

### Step 7 — Create streams.yaml

~2 min (plus ~1 min camera cooldown after go2rtc restart)

```bash
cp go2rtc/streams.yaml.example go2rtc/streams.yaml
```

Edit `go2rtc/streams.yaml` — replace each placeholder with your real values:
```yaml
streams:
  ec6: "xiaomi://<XIAOMI_USER_ID>:<REGION>@<CAMERA_IP>?did=<DEVICE_DID>&model=chuangmi.camera.068ac1"
  ec6_2: "xiaomi://<XIAOMI_USER_ID>:<REGION>@<CAMERA_IP>?did=<DEVICE_DID>&model=chuangmi.camera.068ac1&channel=2"
```

- `ec6` = carport lens
- `ec6_2` = street lens (channel=2)

`go2rtc/streams.yaml` is gitignored (contains credentials).

Restart go2rtc to load the new config:
```bash
# Press Ctrl+C in the Terminal, then:
cd go2rtc && ./go2rtc -config go2rtc.yaml -config streams.yaml
```

> **go2rtc cooldown:** The camera needs ~1 minute after a go2rtc restart before the stream is ready.

✅ Works when: `curl -s http://127.0.0.1:1984/api/streams` returns JSON that includes `ec6` and `ec6_2`.

---

### Step 8 — Test the RTSP streams

~2 min (after the 1-min camera cooldown from Step 7)

```bash
# Carport lens
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/ec6 -frames:v 1 /tmp/test-carport.jpg
# Street lens
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/ec6_2 -frames:v 1 /tmp/test-street.jpg
```

Or view the streams in the go2rtc web UI at **http://127.0.0.1:1984**.

✅ Works when: Both files exist at `/tmp/test-carport.jpg` and `/tmp/test-street.jpg` and are larger than 10 KB.

---

### Step 9 — Get a Claude Code OAuth token

~2 min

> **This step requires the user — do not perform it through an AI agent. The token must not appear in any chat log.**

Open a new Terminal window on the Mac:
```bash
claude setup-token
```

A browser opens for login. After login, the terminal prints a token starting with `sk-ant-oat01-...`. Copy that token — you will paste it into `.env` in the next step.

This token uses quota from your Claude subscription (not an Anthropic API key; no extra charge). It is valid for ~1 year. If it leaks, revoke it at claude.ai settings.

✅ Works when: You have the `sk-ant-oat01-...` token copied and ready to paste.

---

### Step 10 — Create a Telegram bot

~5 min

1. Message **@BotFather** on Telegram → send `/newbot` → follow prompts → receive a `bot token`.
2. Send any message to your new bot (required — Telegram cannot deliver to a chat that has never messaged the bot).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → find `chat.id` in the JSON.

✅ Works when: You have both the bot token and the chat ID ready.

---

### Step 11 — Configure .env

~3 min

```bash
cp .env.example .env
```

Edit `.env` — do not commit this file:
```dotenv
CAM_STREAMS=street=rtsp://host.docker.internal:8554/ec6_2,carport=rtsp://host.docker.internal:8554/ec6

CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # from Step 9
CLAUDE_MODEL=sonnet                          # or haiku (lower quota usage)

TELEGRAM_BOT_TOKEN=...                       # from Step 10
TELEGRAM_CHAT_ID=...

DASHBOARD_PASSWORD=choose-a-strong-password
DASHBOARD_SECRET=    # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
```

All other keys are optional — defaults work. See [Configuration Reference](#configuration-reference).

✅ Works when: `for KEY in CLAUDE_CODE_OAUTH_TOKEN TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DASHBOARD_PASSWORD; do grep -qE "^${KEY}=.+" .env && echo "${KEY}: set" || echo "${KEY}: MISSING"; done` — all four lines say `set`.

---

### Step 12 — Build and start Docker Compose

~5–10 min (first build downloads npm packages incl. the `claude` CLI)

```bash
docker compose up -d --build
```

Watch logs:
```bash
docker compose logs -f monitor
docker compose logs -f dashboard
```

✅ Works when: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/healthz` returns `200`.

---

### Step 13 — Open the dashboard

~1 min

```
http://localhost:8080
# or http://<mac-ip>:8080 from another device on the LAN
```

Enter `DASHBOARD_PASSWORD`. If you see HTTP 503, `DASHBOARD_PASSWORD` is missing from `.env` — fill it and run `docker compose up -d`.

✅ Works when: The dashboard loads and shows the main page after entering your password.

---

### Step 14 — Draw alert lines

~3 min

Open the **"Alert Lines"** page (top menu) → the page shows the latest snapshot for each lens → drag to draw a polyline on the image → Save:

- **WARNING line** (amber `#f59e0b`) — the level at which you want a warning alert (suggested: front gate or fence line)
- **CRITICAL line** (red `#ef4444`) — the critical level (suggested: carport floor or entry)

Draw lines for both the `street` and `carport` lenses.

✅ Works when: The `/lines` page shows saved polylines overlaid on the snapshots for both lenses.

---

### Step 15 — Prevent Mac sleep

~2 min

**Do now (current session only):**
```bash
caffeinate -s
```

**Permanent (requires sudo):**
```bash
sudo pmset -a sleep 0
```

Or: System Settings → Battery / Energy Saver → enable "Prevent automatic sleeping when display is off".

Also set Docker Desktop to **"Start at Login"** in its preferences.

✅ Works when: `pmset -g | grep "^sleep"` shows `sleep  0` (if you used the permanent option).

### Step 16 — Optional: connect a Zigbee siren

~10 min. Skip this step if you have no siren. You need zigbee2mqtt and an MQTT broker already running on your LAN.

1. Find the siren's friendly name in the zigbee2mqtt web UI (for example `Siren`).
2. Add the broker login to `.env`:
   ```dotenv
   MQTT_HOST=<BROKER_IP>
   MQTT_PORT=1883
   MQTT_USERNAME=...
   MQTT_PASSWORD=...
   SIREN_TOPIC=zigbee2mqtt/Siren/set
   ```
3. Run `docker compose up -d`.
4. Dashboard → Settings → **Siren (MQTT)** → press **TEST SIREN (1 S)**. It works before the siren is enabled.
5. Tick `siren_enabled` and save.

On CRITICAL the monitor publishes `{"alarm": true}`, then `{"alarm": false}` after `siren_seconds` (default 60). It repeats on the same schedule as the Telegram critical alert (`critical_repeat_minutes`). WARNING sounds it only when `siren_on_warning` is on. Recovery to normal switches it off. Every siren publish is listed on the Alerts page. Tested with a Tuya TS0216 siren; other sirens need a different payload.

✅ Works when: the test button makes the siren sound for 1 second and the page shows a success message.

---

## Daily Use

### Dashboard pages

| URL | What it shows |
|-----|--------------|
| `/` | Latest status, water-level + rain graph, most recent snapshots |
| `/timeline` | Image timeline for every reading with `level_index` |
| `/reading/{id}` | Detail view for a single reading |
| `/alerts` | Full alert history |
| `/settings` | Edit settings, send a test Telegram message |
| `/lines` | Draw warning/critical lines on snapshots |
| `/healthz` | Health check endpoint (no login required) |

Press **"▶ RUN NOW"** on the main page (`/`) to trigger an immediate capture without waiting for the interval.

### Stop / start / update

```bash
# Stop everything
docker compose down

# Start
docker compose up -d

# Update code
git pull
docker compose up -d --build

# After editing .env
docker compose up -d   # recreates containers
```

### View logs

```bash
docker compose logs -f monitor      # monitor + capture + analysis
docker compose logs -f dashboard    # FastAPI dashboard
cat logs/go2rtc.log                 # go2rtc (if launched via launchd)
```

---

## Configuration Reference

### Environment variables (.env)

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

### Settings editable from the dashboard (stored in SQLite, override .env)

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

---

## How It Works

### Capture cycle (runs every `capture_interval_minutes` minutes)

```
1. Capture    ffmpeg -rtsp_transport tcp -i rtsp://host.docker.internal:8554/<stream>
                └─ -skip_frame nokey  (prevents gray frames from HEVC streams)
                └─ reject frame if luma stddev < 8 (flat/gray) → retry once

2. Overlay    draw warning (amber) / critical (red) lines onto the image
              (only when lines exist for that lens)
              saved as <timestamp>-<label>-lines.jpg

3. Analysis   claude -p --input-format stream-json --output-format stream-json
                └─ sends both overlay images (base64) in a single user message
                └─ uses --json-schema to enforce structured output
                └─ Claude returns: level_status, level_index, per_lens (line_position), etc.

4. Status     derive_final_status():
                └─ if level_index >= level_critical → critical
                └─ if level_index >= level_warning  → warning
                └─ if any lens line_position == at_or_above_critical → escalate → critical
                └─ if any lens line_position == at_or_above_warning  → escalate → warning

5. Store      INSERT INTO readings + lens_readings (SQLite)

6. Alert      compare with previous status → send Telegram if status changed or critical repeat
```

### Water level index (0–100)

| level_index | Meaning |
|-------------|---------|
| 0 | Dry road, no water |
| 25 | Water on the street outside the fence only |
| 50 | Water at the fence line or front gate |
| 75 | Water beginning to enter the carport |
| 100 | Carport floor flooded, or water reaching the car wheels |

- `level_warning` (default 50): alert fires when `level_index` ≥ this value
- `level_critical` (default 90): critical alert + repeat every `critical_repeat_minutes` minutes
- When `critical_confirm` is on (default), the first CRITICAL reading schedules a re-check after `critical_confirm_recheck_seconds` (default 120 s) instead of alerting immediately; only a second consecutive CRITICAL sounds the siren and sends the Telegram message. A "pending" row appears on the Alerts page. If the re-check returns a lower status, the alarm is silently cancelled.

### Alert lines and `line_position`

Users draw a polyline (coordinates normalized 0–1) on a real snapshot via the `/lines` page.

| `line_position` | Meaning |
|-----------------|---------|
| `no_lines` | No lines drawn for this lens |
| `not_visible` | Lines exist but Claude cannot detect the waterline |
| `below_warning` | Water is below the warning line (or no water) |
| `at_or_above_warning` | Water is at or above the warning line |
| `at_or_above_critical` | Water is at or above the critical line |

**Escalation:** If any lens reports `at_or_above_critical`, the final status is at least `critical` — even if `level_index` is below the threshold.

### Claude cost

Measured in production: ~6,000 input + ~1,000 output tokens per cycle, ~14 seconds per run.

At a 10-minute interval = 144 cycles/day ≈ $0.02 API-equivalent per cycle (model `sonnet`).

Switch to `haiku` or increase `capture_interval_minutes` to reduce quota usage.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Reading status `UNKNOWN` + error "Not logged in" | `CLAUDE_CODE_OAUTH_TOKEN` is wrong or expired | Run `claude setup-token` again → update `.env` → `docker compose up -d` |
| Dashboard returns HTTP 503 | `DASHBOARD_PASSWORD` is empty | Set `DASHBOARD_PASSWORD` in `.env` → `docker compose up -d` |
| No Telegram messages received | Token/Chat ID wrong, or user never messaged the bot first | Press "SEND TEST MESSAGE" in Settings; confirm you sent the bot a message |
| ffmpeg timeout or gray frame | go2rtc not ready after restart, or wrong stream URL | Wait ~1 min after go2rtc restart; check `curl http://127.0.0.1:1984/api/streams` |
| go2rtc API not responding | go2rtc is not running | Start go2rtc from a Terminal window |
| Container cannot reach host | Docker Desktop does not resolve `host.docker.internal` | `docker run --rm busybox nc -z -w 3 host.docker.internal 8554` — must exit 0 |
| Port 8080 already in use | Another app is using that port | Set `DASHBOARD_PORT=<other>` in `.env` |
| go2rtc API responds but stream does not work | Camera IP changed, wrong region, or wrong DID | Check `streams.yaml`; re-run `curl "http://127.0.0.1:1984/api/xiaomi?..."` |
| All readings `UNKNOWN` + "All snapshot captures failed" | go2rtc cannot connect to camera (Local Network permission) | See [Known Issues](#known-issues) |
| Siren test says `MQTT connect failed: Not authorized` | Wrong or missing MQTT login | Fix `MQTT_USERNAME` / `MQTT_PASSWORD` in `.env`, then `docker compose up -d` |
| Siren test succeeds but no sound | Wrong topic or friendly name | Match `SIREN_TOPIC` to `zigbee2mqtt/<friendly name>/set` |

---

## Known Issues

1. **go2rtc under launchd or Docker cannot connect to the camera.** When go2rtc is launched by launchd (via `install.sh go2rtc`) or run inside a Docker container, the camera does not respond. Suspected cause: macOS **Local Network privacy permission** (System Settings → Privacy & Security → Local Network). This has not been confirmed — go2rtc launched directly from a Terminal works normally. **Workaround:** Run go2rtc from a Terminal window directly (or `nohup ./go2rtc ... &`) and restart it manually after a reboot. This issue is unresolved.

2. **The Mac must not sleep.** Both go2rtc and Docker must run continuously. If the Mac sleeps, all monitoring stops.

3. **Claude subscription quota.** The system uses Claude subscription quota, not API billing. If quota runs out (rate limit), readings error. Use model `haiku` or increase `capture_interval_minutes` to reduce consumption.

4. **Night IR misread.** In night/IR grayscale mode, Claude may misread water level — wet, reflective road can look like standing water. Check the timeline in the dashboard to verify accuracy.

5. **First frame gray.** After a fresh go2rtc start, the first HEVC frame may be gray (incomplete decode). The capture step handles this with `-skip_frame nokey` and rejects frames with luma stddev < 8, then retries once. Still, wait ~1 min after a go2rtc restart before testing.

---

## Security — What not to commit

| File | Why |
|------|-----|
| `.env` | OAuth token, Telegram token, dashboard password |
| `go2rtc/go2rtc.yaml` | Xiaomi cloud token |
| `go2rtc/streams.yaml` | User ID, Device DID, camera IP |
| `go2rtc/go2rtc` | Binary (large) |
| `data/` | SQLite database (private readings) |
| `snapshots/` | Photos of the property |
| `logs/` | Logs that may contain URLs or IPs |

All of these are already in `.gitignore`. Before committing: `git status` and `git check-ignore -v <file>`.

---

## Development & Tests

```bash
# Create virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Run tests
.venv/bin/python -m pytest -q

# Single run with existing images (no camera needed):
WLM_DB=/tmp/test.db .venv/bin/python -m wlm.runner --dry-run \
  --image street=snapshots/YYYYMMDD-HHMMSS-street.jpg \
  --image carport=snapshots/YYYYMMDD-HHMMSS-carport.jpg

# Single live run (requires .env and go2rtc running):
.venv/bin/python -m wlm.runner --once

# Test Telegram:
.venv/bin/python -m wlm.runner --test-telegram

# Snapshot only (no analysis):
.venv/bin/python -m wlm.runner --snapshot-only
```

### CLI flags (`python -m wlm.runner`)

| Flag | Description |
|------|-------------|
| `--once` | Run one cycle then exit (default) |
| `--loop` | Run on a loop per `capture_interval_minutes` |
| `--dry-run` | Skip Telegram send and state writes (for testing) |
| `--image LABEL=PATH` | Use the given image instead of live capture (repeatable) |
| `--snapshot-only` | Capture + print path then exit (no analysis) |
| `--test-telegram` | Send a Telegram test message then exit |
| `--test-siren` | Sound the siren for 1 s (on, wait 1 s, off) then exit |

### Package structure

```
wlm/
├── db.py          — SQLite schema + helpers
├── settings.py    — Settings resolver: DB → env → default
├── lines.py       — get_lines/set_lines (alert line coordinates)
├── overlay.py     — draw_lines: draws warning/critical lines onto images
├── capture.py     — ffmpeg capture, image metrics, composite builder
├── analysis.py    — Claude Code CLI analysis (stream-json)
├── telegram.py    — Telegram notifications
├── alerts.py      — Alert policy + escalation from line_position
├── weather.py     — Open-Meteo rainfall data
└── runner.py      — Orchestrator + CLI entry point

web/
├── app.py         — FastAPI dashboard routes
└── metrics.py     — Chart data aggregation
```

---

## License

MIT — see [`LICENSE`](LICENSE). Bundled fonts (SIL OFL 1.1) and chart libraries (MIT) keep their own licenses; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). go2rtc is downloaded separately and is MIT-licensed.

---

## 🤖 For AI Agents: Automated Install

This is an ordered runbook for a Claude Code agent (or similar AI coding agent) to install and verify the project end-to-end. Each step lists: goal, exact commands, a verification command with expected output, and what to do on failure.

**Agent rules — read before executing anything:**

- Never read, print, display, log, or commit the contents of `.env`, `go2rtc/go2rtc.yaml`, or `go2rtc/streams.yaml`. They contain secrets.
- Check whether a secret key is set with `grep -qE '^KEY=.+' .env && echo "set" || echo "missing"` — never reveal the value.
- Never `git add` a gitignored file. Run `git check-ignore -v <file>` when in doubt.
- Ask the user before any destructive action (deleting data, stopping services, force-pushing).
- At every HUMAN-REQUIRED step: stop, explain clearly what the user needs to do and why, wait for confirmation before continuing.

---

### Step 1 — Verify working directory

**Goal:** Confirm we are in the project root.

```bash
ls compose.yaml wlm/runner.py go2rtc/streams.yaml.example
```

**Expected:** All three files listed without error.
**On failure:** `cd` to the project directory or ask the user.

---

### Step 2 — Detect architecture

**Goal:** Choose the correct go2rtc binary URL.

```bash
uname -m
```

**Expected output:** `arm64` (Apple Silicon) or `x86_64` (Intel).
**Action:** Set `ARCH_SUFFIX` = `mac_arm64` for arm64, `mac_amd64` for x86_64.

---

### Step 3 — Verify Docker Desktop is running

**Goal:** Ensure Docker is available before building.

```bash
docker info --format '{{.ServerVersion}}' 2>&1 | head -1
```

**Expected:** A version string like `27.x.x`.
**On failure:** Ask the user to start Docker Desktop and wait for it to be ready, then retry.

---

### Step 4 — Install go2rtc binary

**Goal:** Place the go2rtc binary in `go2rtc/`.

```bash
GO2RTC_VERSION="v1.9.14"
ARCH_SUFFIX="mac_arm64"   # replace with mac_amd64 if Step 2 said x86_64
curl -L "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_${ARCH_SUFFIX}.zip" \
  -o /tmp/go2rtc.zip
unzip -o /tmp/go2rtc.zip -d go2rtc/
xattr -d com.apple.quarantine go2rtc/go2rtc 2>/dev/null || true
chmod +x go2rtc/go2rtc
```

**Verification:**
```bash
go2rtc/go2rtc --version 2>&1 | head -1
```
**Expected:** `go2rtc version v1.9.x` or similar.

---

### Step 5 — Create go2rtc.yaml from template

**Goal:** Set up the go2rtc config file with api/rtsp listen settings.

```bash
test -f go2rtc/go2rtc.yaml && echo "exists" || cp go2rtc/go2rtc.yaml.example go2rtc/go2rtc.yaml
```

**Verification:**
```bash
grep -q 'listen: "127.0.0.1:1984"' go2rtc/go2rtc.yaml && echo "ok"
```
**Expected:** `ok`

**Confirm the file is gitignored:**
```bash
git check-ignore -v go2rtc/go2rtc.yaml
```
**Expected:** A line showing `go2rtc/go2rtc.yaml` is ignored. If it is NOT ignored, stop and report.

---

### Step 6 — Start go2rtc

**Goal:** Launch go2rtc so the Xiaomi login UI is reachable.

Note: `streams.yaml` may not exist yet; go2rtc will warn but start normally.

```bash
mkdir -p logs
nohup go2rtc/go2rtc -config go2rtc/go2rtc.yaml 2>>logs/go2rtc.log &
sleep 3
```

**Verification:**
```bash
curl -s http://127.0.0.1:1984/api/streams | head -c 80
```
**Expected:** JSON (possibly `{}` or `null`) with no connection error.
**On failure:** Check `logs/go2rtc.log`; port 1984 may be in use by another process.

---

### Step 7 — HUMAN-REQUIRED: Xiaomi login

**Stop and ask the user to do the following:**

1. Open **http://127.0.0.1:1984** in a browser.
2. Click **"Add"** → select **"Xiaomi"**.
3. Log in with the Mi Home account the camera is registered under (may require captcha or 2FA code sent to their phone).
4. After successful login, go2rtc writes a `xiaomi:` section with the cloud token into `go2rtc/go2rtc.yaml`.

Do **not** read or display `go2rtc/go2rtc.yaml` after this step.

**Verification (run without displaying the file):**
```bash
grep -q 'xiaomi:' go2rtc/go2rtc.yaml && echo "token saved" || echo "NOT saved"
```
**Expected:** `token saved`

---

### Step 8 — Find Xiaomi User ID and list devices

**Goal:** Discover `XIAOMI_USER_ID`, `REGION`, and `DEVICE_DID` for the camera.

Ask the user for their Mi Home User ID (visible in Mi Home app → profile). Then run the device listing for common regions. Replace `<XIAOMI_USER_ID>` with the actual value provided by the user.

```bash
for REGION in sg cn de i2 ru us; do
  echo "=== region: $REGION ===" 
  curl -s "http://127.0.0.1:1984/api/xiaomi?id=<XIAOMI_USER_ID>&region=${REGION}" | python3 -m json.tool 2>/dev/null | grep -E '"did"|"model"|"name"' | head -20
done
```

Show the output to the user and ask them to identify which device is their IMILAB EC6 Dual (model `chuangmi.camera.068ac1`). Record `DEVICE_DID` and `REGION` for Step 9.

---

### Step 9 — HUMAN-REQUIRED: Create streams.yaml

**Stop and ask the user to provide:**
- `<CAMERA_IP>` (from Mi Home or DHCP list)
- `<XIAOMI_USER_ID>`
- `<DEVICE_DID>` (from Step 8 output)
- `<REGION>` (from Step 8 output)

Then create `go2rtc/streams.yaml` from the template. The agent must substitute placeholders but must NOT include the actual values in its chat response.

```bash
sed \
  -e 's|<XIAOMI_USER_ID>|'"$XIAOMI_USER_ID"'|g' \
  -e 's|<REGION>|'"$REGION"'|g' \
  -e 's|<CAMERA_IP>|'"$CAMERA_IP"'|g' \
  -e 's|<DEVICE_DID>|'"$DEVICE_DID"'|g' \
  go2rtc/streams.yaml.example > go2rtc/streams.yaml
```

**Confirm streams.yaml is gitignored:**
```bash
git check-ignore -v go2rtc/streams.yaml
```
**Expected:** gitignored. If NOT, stop and report.

Restart go2rtc to pick up the new streams.yaml:
```bash
pkill -f "go2rtc -config" 2>/dev/null || true
sleep 2
nohup go2rtc/go2rtc -config go2rtc/go2rtc.yaml -config go2rtc/streams.yaml 2>>logs/go2rtc.log &
sleep 5
```

Wait ~60 seconds before testing streams (camera cooldown after go2rtc restart).

---

### Step 10 — Test RTSP streams

**Goal:** Confirm both lens streams produce a valid JPEG frame.

```bash
sleep 60   # wait for camera cooldown
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/ec6 \
  -frames:v 1 /tmp/wlm-test-ec6.jpg -y 2>&1 | tail -3
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/ec6_2 \
  -frames:v 1 /tmp/wlm-test-ec6_2.jpg -y 2>&1 | tail -3
ls -lh /tmp/wlm-test-ec6.jpg /tmp/wlm-test-ec6_2.jpg
```

**Expected:** Both files exist and are >10 KB.
**On failure:** Check `logs/go2rtc.log` for stream errors; verify `CAMERA_IP` / `DEVICE_DID` / `REGION`; retry after another 60 seconds.

---

### Step 11 — HUMAN-REQUIRED: claude setup-token

**Stop and ask the user to do the following in their own Terminal window (not through the agent):**

```
claude setup-token
```

This opens a browser to log in to Claude. After login, the terminal prints a token starting with `sk-ant-oat01-...`. The user must copy this token.

**Reason for human-only step:** The token must never appear in agent chat logs.

Ask the user to confirm they have the token ready (they do not need to show it to you).

---

### Step 12 — HUMAN-REQUIRED: Create Telegram bot

**Stop and ask the user to:**

1. Message **@BotFather** on Telegram → `/newbot` → follow prompts → receive bot token.
2. Send any message to the new bot (required — otherwise Telegram cannot deliver messages to the chat).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find `chat.id` in the JSON.

Ask the user to confirm they have: bot token, chat id.

---

### Step 13 — Create and populate .env

**Goal:** Create `.env` with required values.

```bash
test -f .env || cp .env.example .env
```

**Ask the user to fill in `.env` directly** (open it in their editor):

Required keys:
- `CLAUDE_CODE_OAUTH_TOKEN` — token from Step 11
- `TELEGRAM_BOT_TOKEN` — from Step 12
- `TELEGRAM_CHAT_ID` — from Step 12
- `DASHBOARD_PASSWORD` — choose a strong password
- `DASHBOARD_SECRET` — run `python3 -c "import secrets; print(secrets.token_hex(32))"` and paste result

After the user confirms they have filled the file, verify presence (not values):

```bash
for KEY in CLAUDE_CODE_OAUTH_TOKEN TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DASHBOARD_PASSWORD; do
  grep -qE "^${KEY}=.+" .env && echo "${KEY}: set" || echo "${KEY}: MISSING"
done
```

**Expected:** All four lines show `set`.
**On failure:** Ask the user to fill in the missing keys.

Confirm `.env` is gitignored:
```bash
git check-ignore -v .env
```
**Expected:** gitignored.

---

### Step 14 — Build and start Docker Compose

**Goal:** Build the container image and start both services.

```bash
docker compose up -d --build
```

**Verification (wait up to 30s for healthy):**
```bash
sleep 15
docker compose ps
```
**Expected:** Both `monitor` and `dashboard` services are `Up` (not `Exit`).

Check dashboard responds:
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/healthz
```
**Expected:** `200` (`/healthz` does not require a password).

Then check the login page:
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/login
```
**Expected:** `200`. **If `/` returns `503`:** `DASHBOARD_PASSWORD` is missing in `.env` — go back to Step 13, then `docker compose up -d`.

---

### Step 15 — Verify container can reach go2rtc on host

**Goal:** Confirm `host.docker.internal` resolves and port 8554 is reachable from inside the container.

```bash
docker run --rm busybox nc -z -w 3 host.docker.internal 8554 && echo "reachable" || echo "UNREACHABLE"
```

**Expected:** `reachable`
**On failure:** go2rtc may not be running, or its RTSP listen is not on 127.0.0.1:8554. Check `logs/go2rtc.log`.

---

### Step 16 — Verify Claude auth inside container

**Goal:** Confirm the OAuth token is present in the running container.

```bash
docker compose exec -T monitor sh -c '[ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] && echo "ok" || echo "MISSING"'
```

**Expected:** `ok`
**On failure:** Token is missing from `.env` or container was not recreated after `.env` change. Run `docker compose up -d` and retry.

---

### Step 17 — Trigger a manual run and check the first reading

**Goal:** Confirm the full capture → analysis → store pipeline works.

Trigger an immediate run via the `run_now_requested` flag:
```bash
sqlite3 data/wlm.db "INSERT INTO runtime_state(key,value) VALUES('run_now_requested','1') ON CONFLICT(key) DO UPDATE SET value='1';"
```

Wait for the run to complete (~20–30 seconds):
```bash
sleep 30
```

Check the latest reading:
```bash
sqlite3 data/wlm.db "SELECT id, status, level_index, substr(error,1,120) FROM readings ORDER BY id DESC LIMIT 1;"
```

**Expected:** A row where `status` is `normal`, `warning`, `critical`, or `unknown` (not empty). If `status` is `unknown`, the `error` column shows the reason.

Common `unknown` causes and fixes:

| Error substring | Fix |
|----------------|-----|
| `Not logged in` | Redo Step 11; update `CLAUDE_CODE_OAUTH_TOKEN` in `.env`; `docker compose up -d` |
| `claude CLI not found` | Dockerfile installs claude via npm — rebuild with `docker compose up -d --build` |
| `claude CLI timed out` | Analysis took >180s; try model `haiku` in `.env` |
| `All snapshot captures failed` (per-lens error `Capture failed`) | go2rtc stream unavailable; confirm `go2rtc` is running and check its log; wait ~1 min after a go2rtc restart |

---

### Step 18 — HUMAN-REQUIRED: Draw alert lines

**Stop and ask the user to:**

1. Open **http://localhost:8080** in a browser and log in.
2. Navigate to **"Alert Lines"** (top menu).
3. Select each lens (`street`, `carport`) and draw:
   - A **WARNING line** (amber) — suggested: at the front gate/fence level
   - A **CRITICAL line** (red) — suggested: at the carport floor/entry level
4. Click Save for each lens.

This step requires human judgment about the physical layout visible in the snapshot.

---

### Step 19 — Keep Mac awake

**Goal:** Ensure the Mac does not sleep (sleep stops all monitoring).

```bash
# Check current sleep setting
pmset -g | grep "^sleep"
```

If the user wants a permanent setting (requires sudo — ask first):
```bash
sudo pmset -a sleep 0
```

Remind the user: Docker Desktop must also be set to **"Start at Login"** in its preferences.

---

### Final Acceptance Checklist

Run each verification and confirm all pass:

```bash
# 1. Both containers running
docker compose ps

# 2. Dashboard healthz
curl -s -o /dev/null -w "dashboard healthz: %{http_code}\n" http://localhost:8080/healthz

# 3. Container reaches host RTSP
docker run --rm busybox nc -z -w 3 host.docker.internal 8554 && echo "rtsp: reachable"

# 4. Latest reading exists and is not an auth error
sqlite3 data/wlm.db "SELECT 'reading id=' || id || ' status=' || status || ' level=' || coalesce(level_index,'null') FROM readings ORDER BY id DESC LIMIT 1;"

# 5. go2rtc API is up
curl -s http://127.0.0.1:1984/api/streams | python3 -m json.tool | grep -E '"ec6|"ec6_2' | head -4

# 6. Secrets are NOT tracked by git
git check-ignore -v .env go2rtc/go2rtc.yaml go2rtc/streams.yaml

# 7. go2rtc.yaml.example IS tracked
git ls-files go2rtc/go2rtc.yaml.example
```

**All green?** The system is operational. Open the dashboard to monitor the first few cycles and verify readings match real-world water levels.
