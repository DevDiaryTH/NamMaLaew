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
