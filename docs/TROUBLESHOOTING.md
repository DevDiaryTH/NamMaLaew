# Troubleshooting — NamMaLaew

← Back to [README](../README.md)

## Symptoms

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
| All readings `UNKNOWN` + "All snapshot captures failed" | go2rtc cannot connect to camera (Local Network permission) | See [Known issues](#known-issues) |
| Siren test says `MQTT connect failed: Not authorized` | Wrong or missing MQTT login | Fix `MQTT_USERNAME` / `MQTT_PASSWORD` in `.env`, then `docker compose up -d` |
| Siren test succeeds but no sound | Wrong topic or friendly name | Match `SIREN_TOPIC` to `zigbee2mqtt/<friendly name>/set` |

## Known issues

1. **go2rtc under launchd or Docker cannot connect to the camera.** When go2rtc is launched by launchd (via `install.sh go2rtc`) or run inside a Docker container, the camera does not respond. Suspected cause: macOS **Local Network privacy permission** (System Settings → Privacy & Security → Local Network). This has not been confirmed — go2rtc launched directly from a Terminal works normally. **Workaround:** Run go2rtc from a Terminal window directly (or `nohup ./go2rtc ... &`) and restart it manually after a reboot. This issue is unresolved.

2. **The Mac must not sleep.** Both go2rtc and Docker must run continuously. If the Mac sleeps, all monitoring stops.

3. **Claude subscription quota.** The system uses Claude subscription quota, not API billing. If quota runs out (rate limit), readings error. Use model `haiku` or increase `capture_interval_minutes` to reduce consumption.

4. **Night IR misread.** In night/IR grayscale mode, Claude may misread water level — wet, reflective road can look like standing water. Check the timeline in the dashboard to verify accuracy.

5. **First frame gray.** After a fresh go2rtc start, the first HEVC frame may be gray (incomplete decode). The capture step handles this with `-skip_frame nokey` and rejects frames with luma stddev < 8, then retries once. Still, wait ~1 min after a go2rtc restart before testing.
