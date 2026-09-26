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

## Human feedback and few-shot learning

After each reading you can open its detail page and leave feedback: mark it **correct** (Claude got it right) or **wrong** (providing the true status and level index). Optionally tick **Use as reference example** to promote the reading's image(s) to a persistent reference set stored under `snapshots/examples/<reading_id>/`.

When examples are present, the runner prepends them to every Claude call as verified ground-truth images:

1. A text preamble explains that these are human-confirmed examples from the same site.
2. Each example appears as a label block + its image(s).
3. A "Current images to analyze:" separator precedes the live frames.
4. One sentence at the end of the prompt tells Claude to use the examples for scale calibration.

The `learning_max_examples` setting (default 3) controls how many examples are sent. Selection picks one example per status level (critical → warning → normal), preferring the same day/night mode as the current capture, then fills remaining slots with the newest examples. Setting `learning_max_examples` to 0 disables the feature entirely.

Reference examples survive `prune_snapshots()` because pruning only removes JPEG files directly in `snapshots/` (non-recursive), while examples are stored in `snapshots/examples/<id>/`.

### Recent-history context

When `learning_use_history` is enabled (default on), the runner adds a short text summary of the last few readings to each Claude call. The summary covers readings from the past 2 hours (up to 6, excluding `unknown`), listed chronologically with time, status, level index, and confidence. If a reading has feedback, the summary uses the human-corrected or human-confirmed values instead and marks them accordingly.

A "Rain in last 3h" line is appended when weather precipitation data is available for the site.

The history block appears in the Claude prompt between the reference examples and the current images:

```
[Reference examples (if any)]
Recent readings at this site (context only):
  HH:MM UTC (-Xm ago)  status=...  level=...  conf=...%
  ...
Rain in last 3h: X.X mm  (if weather data present)
Current images to analyze:
[Current lens images]
[Analysis prompt — includes a paragraph asking Claude to treat the history as a soft prior]
```

Claude is explicitly instructed to judge each set of images on their own visual evidence first, and to use the history only as a prior: a clear change in the images should be reported even if it is large; history only nudges the result when the image evidence is ambiguous. Set `learning_use_history` to `0` to disable.

### Learned rain-to-water response

Once latitude/longitude are set, the monitor keeps hourly rainfall in the `weather` table (past 2 days plus a 2-day forecast, refreshed every 30 min). After each cycle it refits, at most once a day, a straight line `Δlevel = a · rain_3h + b`. The line comes from the site's own history:

- each sample pairs the rain that fell in the 3 hours before an hour H with the change in `level_index` from H to H+2h;
- the level used is the reading nearest to each time, within ±15 min;
- human-corrected levels from feedback replace the model's values.

The fit is used only after at least 20 samples with rain. Until then the overview shows how many rain events have been collected ("k/20"). The result is stored in `runtime_state` under `rain_model`.

With a fitted model, `predict_rise` sums the forecast rain for the next 3 whole hours. It then predicts the rise as `max(0, a · rain + b)`, and the overview's rain tile shows it together with the number of events and R². The model was fitted on the rise over the 2 hours after the rain, so read the prediction as "roughly how much this rain will lift the water", not as an exact time. When the recent-trend ETA is unavailable, the overview shows a rain-based ETA instead (labelled "rain forecast").

The early warning (`alert_on_forecast`, default off) sends a Telegram message when the current level is below `level_warning` and the predicted level reaches it. It sends at most one message every 3 hours. It never changes the reading's status and never sounds the siren.

### Calibrated confidence

Claude returns a `confidence` value (0–1) with every reading, but self-reported confidence is not always well-calibrated — a model may say 0.9 when it is actually right only 70% of the time in that range.

The monitor collects human feedback (correct / wrong) and groups past readings into four confidence buckets: 0–50%, 50–70%, 70–85%, 85–100%. For each bucket it computes the observed accuracy (fraction of readings where the model's status matched the human verdict). Once a bucket has at least 5 samples it is "reliable" and its observed accuracy replaces the raw confidence as the `calibrated_confidence` value stored on the reading.

`calibrated_confidence` is used in two ways:

1. **Dashboard display** — the overview shows the calibrated value (labelled "CALIBRATED") when it is available, and an "UNCERTAIN" badge when it is below `min_confidence`. The reading detail page shows both raw and calibrated values side by side.

2. **Alert gating** — when `min_confidence > 0` and a CRITICAL reading's calibrated confidence is below that threshold, the reading enters the same pending/re-check path as `critical_confirm` (even when `critical_confirm` is off). A second CRITICAL reading is required before the siren sounds and the Telegram message is sent. Low confidence never lowers a status, never suppresses a confirmed or sustained critical, and has no effect on warning, normal, or unknown readings.

The Settings page's Accuracy Statistics card shows the overall status accuracy, mean absolute level error, and the per-bucket calibration table.
