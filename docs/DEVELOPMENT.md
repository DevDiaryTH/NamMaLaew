# Development & Tests — NamMaLaew

← Back to [README](../README.md)

## Commands

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

## CLI flags (`python -m wlm.runner`)

| Flag | Description |
|------|-------------|
| `--once` | Run one cycle then exit (default) |
| `--loop` | Run on a loop per `capture_interval_minutes` |
| `--dry-run` | Skip Telegram send and state writes (for testing) |
| `--image LABEL=PATH` | Use the given image instead of live capture (repeatable) |
| `--snapshot-only` | Capture + print path then exit (no analysis) |
| `--test-telegram` | Send a Telegram test message then exit |
| `--test-siren` | Sound the siren for 1 s (on, wait 1 s, off) then exit |

## Package structure

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
