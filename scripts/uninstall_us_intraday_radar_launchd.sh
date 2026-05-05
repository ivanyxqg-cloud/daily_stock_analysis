#!/bin/zsh
set -euo pipefail

LABEL="${US_INTRADAY_LAUNCHD_LABEL:-com.ivanyxqg.us-intraday-radar}"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_SUPPORT_DIR="$HOME/Library/Application Support/us-intraday-radar"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
rm -f "$APP_SUPPORT_DIR/us_intraday_radar_watchdog.sh"
rmdir "$APP_SUPPORT_DIR" >/dev/null 2>&1 || true

echo "Uninstalled $LABEL"
