# 🤖 AI Agent Install Runbook — NamMaLaew

← Back to [README](../README.md)

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
2. Navigate to **"LINE EDITOR"** (top menu, `/lines`).
3. Pick the lens that should decide CRITICAL and draw:
   - A **CRITICAL line** (red) — where water becomes dangerous, on the side the water actually arrives from
   - A **WARNING line** (amber) — where water becomes worth a warning
4. Click Save for each lens.
5. Open **SETTINGS** → **SET UP FOR YOUR SITE** and finish the checklist, including **Reference description** (what each lens shows, which lens decides CRITICAL, what 0 / 50 / 100 look like).

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
