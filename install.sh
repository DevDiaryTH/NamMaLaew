#!/usr/bin/env bash
# install.sh — set up the water-level-monitor project and register launchd agents.
# Usage:
#   bash install.sh           — install both agents (go2rtc first, then monitor)
#   bash install.sh go2rtc    — install / reinstall only the go2rtc agent
#   bash install.sh monitor   — install / reinstall only the monitor agent
# Run once after cloning/downloading the project.  launchd will start each agent
# automatically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
USER_UID="$(id -u)"

# ---- parse optional argument ----
INSTALL_TARGET="${1:-both}"
case "$INSTALL_TARGET" in
    go2rtc|monitor|both) ;;
    *) echo "Usage: $0 [go2rtc|monitor]" >&2; exit 1 ;;
esac

echo "==> NamMaLaew — installer (target: $INSTALL_TARGET)"
echo "    Project dir: $SCRIPT_DIR"

# ---- Python venv + deps (only needed for the monitor agent) ----
if [ "$INSTALL_TARGET" != "go2rtc" ]; then
    if [ ! -d "$SCRIPT_DIR/.venv" ]; then
        echo "==> Creating Python virtual environment at .venv ..."
        /usr/bin/python3 -m venv "$SCRIPT_DIR/.venv"
    fi

    echo "==> Installing Python dependencies ..."
    "$SCRIPT_DIR/.venv/bin/pip" install --quiet --upgrade pip
    "$SCRIPT_DIR/.venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

    # ---- .env ----
    if [ ! -f "$SCRIPT_DIR/.env" ]; then
        echo "==> Creating .env from .env.example — please fill in your credentials."
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    fi
fi

# ---- shared directories ----
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SCRIPT_DIR/snapshots"
mkdir -p "$LAUNCH_AGENTS_DIR"

# ---- helper: install one launchd agent ----
_install_agent() {
    local label="$1"
    local plist_src="$SCRIPT_DIR/$label.plist"
    local plist_dest="$LAUNCH_AGENTS_DIR/$label.plist"

    echo "==> Installing $label plist to $plist_dest ..."
    sed "s|___PROJECT_DIR___|$SCRIPT_DIR|g" "$plist_src" > "$plist_dest"

    echo "==> Bootstrapping launchd agent (gui/$USER_UID/$label) ..."
    # Unload first in case an older version is running
    launchctl bootout "gui/$USER_UID/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$USER_UID" "$plist_dest"
}

# ---- go2rtc agent ----
if [ "$INSTALL_TARGET" = "go2rtc" ] || [ "$INSTALL_TARGET" = "both" ]; then
    echo "==> Stopping any manually-started go2rtc instance ..."
    pkill -f "go2rtc -config" 2>/dev/null || true
    _install_agent "com.user.go2rtc"
fi

# ---- monitor agent ----
if [ "$INSTALL_TARGET" = "monitor" ] || [ "$INSTALL_TARGET" = "both" ]; then
    _install_agent "com.user.water-level-monitor"
fi

echo ""
echo "✅  Installation complete (target: $INSTALL_TARGET)."

if [ "$INSTALL_TARGET" != "go2rtc" ]; then
    echo ""
    echo "Next steps:"
    echo "  1. Edit $SCRIPT_DIR/.env and fill in all required values."
    echo "  2. Run a quick smoke test:"
    echo "       $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/monitor.py --dry-run"
    echo "  3. Test Telegram connectivity:"
    echo "       $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/monitor.py --test-telegram"
fi
echo ""
echo "Note: launchd agents do NOT run while the Mac is asleep."
echo "      Keep your Mac awake with: sudo pmset -a sleep 0"
echo "      or enable 'Prevent automatic sleeping when display is off' in"
echo "      System Settings → Battery / Energy Saver."
