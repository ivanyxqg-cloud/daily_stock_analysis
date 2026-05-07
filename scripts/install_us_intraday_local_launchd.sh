#!/bin/zsh
set -euo pipefail

LABEL="${US_INTRADAY_LOCAL_LAUNCHD_LABEL:-com.ivanyxqg.us-intraday-radar-local}"
INTERVAL_SECONDS="${US_INTRADAY_LOCAL_INTERVAL_SECONDS:-60}"
SCRIPT_DIR="${0:A:h}"
REPO_DIR="${US_INTRADAY_REPO_DIR:-${SCRIPT_DIR:h}}"
SOURCE_RUNNER_SCRIPT="$SCRIPT_DIR/us_intraday_local_runner.sh"
APP_SUPPORT_DIR="$HOME/Library/Application Support/us-intraday-radar"
RUNNER_SCRIPT="$APP_SUPPORT_DIR/us_intraday_local_runner.sh"
VENV_DIR="$APP_SUPPORT_DIR/venv"
BOOTSTRAP_PYTHON="${US_INTRADAY_BOOTSTRAP_PYTHON:-}"
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
STDOUT_LOG="$LOG_DIR/us-intraday-radar-local.log"
STDERR_LOG="$LOG_DIR/us-intraday-radar-local.err.log"

mkdir -p "$APP_SUPPORT_DIR" "$PLIST_DIR" "$LOG_DIR"
cp "$SOURCE_RUNNER_SCRIPT" "$RUNNER_SCRIPT"
chmod +x "$RUNNER_SCRIPT"

if [ -z "$BOOTSTRAP_PYTHON" ]; then
  BOOTSTRAP_PYTHON="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)"
fi

if [ -x "$VENV_DIR/bin/python" ]; then
  if ! "$VENV_DIR/bin/python" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  then
    rm -rf "$VENV_DIR"
  fi
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"

if ! command -v m2f >/dev/null 2>&1; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found; Telegram will fall back to text if image conversion is unavailable." >&2
  else
    npm install -g markdown-to-file
  fi
fi

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true

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
    <string>$RUNNER_SCRIPT</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>US_INTRADAY_REPO_DIR</key>
    <string>$REPO_DIR</string>
    <key>US_INTRADAY_APP_SUPPORT_DIR</key>
    <string>$APP_SUPPORT_DIR</string>
    <key>US_INTRADAY_PYTHON_BIN</key>
    <string>$VENV_DIR/bin/python</string>
    <key>US_INTRADAY_WINDOW</key>
    <string>auto</string>
    <key>US_INTRADAY_FORCE_RUN</key>
    <string>false</string>
    <key>MARKDOWN_TO_IMAGE_CHANNELS</key>
    <string>telegram</string>
    <key>MD2IMG_ENGINE</key>
    <string>markdown-to-file</string>
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

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed $LABEL"
echo "Plist: $PLIST_PATH"
echo "Runner: $RUNNER_SCRIPT"
echo "Python: $VENV_DIR/bin/python"
echo "Log: $STDOUT_LOG"
echo "Error log: $STDERR_LOG"
