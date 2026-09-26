#!/usr/bin/env bash
# uninstall.sh — remove launchd agents for the water-level-monitor project.
# Usage:
#   bash uninstall.sh           — remove both agents (monitor first, then go2rtc)
#   bash uninstall.sh monitor   — remove only the monitor agent
#   bash uninstall.sh go2rtc    — remove only the go2rtc agent
# Does NOT delete the project directory, .env, snapshots, or logs.
set -euo pipefail

USER_UID="$(id -u)"

# ---- parse optional argument ----
UNINSTALL_TARGET="${1:-both}"
case "$UNINSTALL_TARGET" in
    go2rtc|monitor|both) ;;
    *) echo "Usage: $0 [go2rtc|monitor]" >&2; exit 1 ;;
esac

echo "==> NamMaLaew — uninstaller (target: $UNINSTALL_TARGET)"

# ---- helper: unload and remove one launchd agent ----
_uninstall_agent() {
    local label="$1"
    local plist_dest="$HOME/Library/LaunchAgents/$label.plist"

    if launchctl list "$label" &>/dev/null; then
        echo "==> Stopping and unloading $label ..."
        launchctl bootout "gui/$USER_UID/$label" 2>/dev/null || \
            launchctl unload "$plist_dest" 2>/dev/null || true
    else
        echo "    $label not currently loaded (skipping bootout)"
    fi

    if [ -f "$plist_dest" ]; then
        echo "==> Removing plist: $plist_dest"
        rm -f "$plist_dest"
    fi
}

if [ "$UNINSTALL_TARGET" = "monitor" ] || [ "$UNINSTALL_TARGET" = "both" ]; then
    _uninstall_agent "com.user.water-level-monitor"
fi

if [ "$UNINSTALL_TARGET" = "go2rtc" ] || [ "$UNINSTALL_TARGET" = "both" ]; then
    _uninstall_agent "com.user.go2rtc"
fi

echo ""
echo "✅  Uninstall complete (target: $UNINSTALL_TARGET)."
echo "    The project directory, .env, snapshots/, and logs/ are unchanged."
echo "    To fully remove: delete the project folder manually."
