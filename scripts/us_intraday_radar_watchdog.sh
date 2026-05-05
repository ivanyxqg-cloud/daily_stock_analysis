#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO_FULL_NAME="${US_INTRADAY_GITHUB_REPO:-ivanyxqg-cloud/daily_stock_analysis}"
WORKFLOW_FILE="${US_INTRADAY_WORKFLOW_FILE:-us_intraday_radar.yml}"
WINDOW="${US_INTRADAY_WINDOW:-auto}"
FORCE_RUN="${US_INTRADAY_FORCE_RUN:-false}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

ny_weekday="$(TZ=America/New_York date +%u)"
ny_hhmm="$(TZ=America/New_York date +%H%M)"
if [ "$ny_weekday" -gt 5 ]; then
  echo "[$(timestamp)] Skipping: New York weekend."
  exit 0
fi

if [[ "$ny_hhmm" < "0920" || "$ny_hhmm" > "1820" ]]; then
  echo "[$(timestamp)] Skipping: outside broad US radar window (09:20-18:20 ET)."
  exit 0
fi

echo "[$(timestamp)] Triggering $WORKFLOW_FILE for $REPO_FULL_NAME window=$WINDOW force_run=$FORCE_RUN"

if ! command -v gh >/dev/null 2>&1; then
  echo "[$(timestamp)] gh CLI not found; install GitHub CLI or disable this LaunchAgent."
  exit 0
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "[$(timestamp)] gh CLI is not authenticated; run gh auth login once."
  exit 0
fi

if gh workflow run "$WORKFLOW_FILE" \
  --repo "$REPO_FULL_NAME" \
  -f window="$WINDOW" \
  -f force_run="$FORCE_RUN"; then
  echo "[$(timestamp)] Workflow trigger submitted."
else
  echo "[$(timestamp)] Workflow trigger failed."
  exit 0
fi
