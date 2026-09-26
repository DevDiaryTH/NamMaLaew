# How It Works — NamMaLaew

← Back to [README](../README.md)

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

**Note:** go2rtc launched via launchd (or inside Docker) fails to connect to the camera on some machines. Suspected cause: macOS Local Network privacy permission. See [Troubleshooting → Known issues](TROUBLESHOOTING.md#known-issues).

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

4. Status     derive_final_status()  (see "Alert lines" below)

5. Store      INSERT INTO readings + lens_readings (SQLite)

6. Alert      compare with previous status → send Telegram if status changed or critical repeat
```

### Water level index (0–100)

The scale comes from each site's **Reference description** setting, which Claude reads on every check. The built-in default only says 0 = dry, 50 = water at the warning line, 100 = water at the critical line; each site should describe its own lenses and landmarks.

- `level_warning` (default 50): WARNING when `level_index` ≥ this value
- `level_critical` (default 90): CRITICAL when `level_index` ≥ this value, **only if no red line is drawn**; repeats every `critical_repeat_minutes` minutes
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

A lens with a red (critical) line is a **deciding lens**. Final status, in order:

1. Claude returned no result → `unknown`
2. A deciding lens failed to capture → `unknown`
3. Any lens reports `at_or_above_critical` → `critical`
4. A deciding lens reports `not_visible` or no `line_position` → `unknown`
5. `level_index` vs `level_warning` / `level_critical`; `at_or_above_warning` lifts `normal` to `warning`
6. With a deciding lens, `critical` from `level_index` alone is capped at `warning`

With no red line anywhere, step 5 decides on its own. `unknown` readings count toward the failure alert (`failure_threshold`, default 3).

**Camera down:** when some (not all) cameras fail to capture `failure_threshold` checks in a row, one Telegram alert names them; another follows when all are back.

### Claude cost

Measured in production: ~6,000 input + ~1,000 output tokens per cycle, ~14 seconds per run.

At a 10-minute interval = 144 cycles/day ≈ $0.02 API-equivalent per cycle (model `sonnet`).

Switch to `haiku` or increase `capture_interval_minutes` to reduce quota usage.
