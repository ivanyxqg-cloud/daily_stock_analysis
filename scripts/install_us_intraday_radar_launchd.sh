#!/bin/zsh
set -euo pipefail

LABEL="${US_INTRADAY_LAUNCHD_LABEL:-com.ivanyxqg.us-intraday-radar}"
REPO_FULL_NAME="${US_INTRADAY_GITHUB_REPO:-ivanyxqg-cloud/daily_stock_analysis}"
INTERVAL_SECONDS="${US_INTRADAY_WATCHDOG_INTERVAL_SECONDS:-300}"
SCRIPT_DIR="${0:A:h}"
WATCHDOG_SCRIPT="$SCRIPT_DIR/us_intraday_radar_watchdog.sh"
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
STDOUT_LOG="$LOG_DIR/us-intraday-radar-watchdog.log"
STDERR_LOG="$LOG_DIR/us-intraday-radar-watchdog.err.log"

mkdir -p "$PLIST_DIR" "$LOG_DIR"
chmod +x "$WATCHDOG_SCRIPT"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$WATCHDOG_SCRIPT</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>US_INTRADAY_GITHUB_REPO</key>
    <string>$REPO_FULL_NAME</string>
    <key>US_INTRADAY_WORKFLOW_FILE</key>
    <string>us_intraday_radar.yml</string>
    <key>US_INTRADAY_WINDOW</key>
    <string>auto</string>
    <key>US_INTRADAY_FORCE_RUN</key>
    <string>false</string>
  </dict>

  <key>StartInterval</key>
  <integer>$INTERVAL_SECONDS</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$STDOUT_LOG</string>

  <key>StandardErrorPath</key>
  <string>$STDERR_LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed $LABEL"
echo "Plist: $PLIST_PATH"
echo "Log: $STDOUT_LOG"
echo "Error log: $STDERR_LOG"
