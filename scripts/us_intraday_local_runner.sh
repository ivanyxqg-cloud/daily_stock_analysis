#!/bin/zsh
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SERVICE_PREFIX="${US_INTRADAY_KEYCHAIN_PREFIX:-us-intraday-radar}"
ACCOUNT="${US_INTRADAY_KEYCHAIN_ACCOUNT:-$USER}"
APP_SUPPORT_DIR="${US_INTRADAY_APP_SUPPORT_DIR:-$HOME/Library/Application Support/us-intraday-radar}"
REPO_DIR="${US_INTRADAY_REPO_DIR:-}"
PYTHON_BIN="${US_INTRADAY_PYTHON_BIN:-$APP_SUPPORT_DIR/venv/bin/python}"
WINDOW="${US_INTRADAY_WINDOW:-auto}"
FORCE_RUN="${US_INTRADAY_FORCE_RUN:-false}"
LOCK_DIR="${US_INTRADAY_LOCAL_LOCK_DIR:-$APP_SUPPORT_DIR/run.lock}"
LOCK_TTL_SECONDS="${US_INTRADAY_LOCAL_LOCK_TTL_SECONDS:-900}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

read_secret() {
  local key="$1"
  local service="$SERVICE_PREFIX.$key"
  security find-generic-password -a "$ACCOUNT" -s "$service" -w 2>/dev/null || true
}

acquire_lock() {
  mkdir -p "$APP_SUPPORT_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    date +%s > "$LOCK_DIR/started_at"
    echo "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
    return 0
  fi

  local now started age
  now="$(date +%s)"
  started="$(cat "$LOCK_DIR/started_at" 2>/dev/null || stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)"
  age=$((now - started))
  if [ "$age" -gt "$LOCK_TTL_SECONDS" ]; then
    echo "[$(timestamp)] Removing stale local radar lock age=${age}s."
    rm -rf "$LOCK_DIR"
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      date +%s > "$LOCK_DIR/started_at"
      echo "$$" > "$LOCK_DIR/pid"
      trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
      return 0
    fi
  fi

  echo "[$(timestamp)] Skipping: previous local radar run is still active."
  exit 0
}

ny_weekday="$(TZ=America/New_York date +%u)"
ny_hhmm="$(TZ=America/New_York date +%H%M)"
if [ "$FORCE_RUN" != "true" ]; then
  if [ "$ny_weekday" -gt 5 ]; then
    echo "[$(timestamp)] Skipping: New York weekend."
    exit 0
  fi
  if [[ "$ny_hhmm" < "0920" || "$ny_hhmm" > "1820" ]]; then
    echo "[$(timestamp)] Skipping: outside broad US radar window (09:20-18:20 ET)."
    exit 0
  fi
fi

if [ -z "$REPO_DIR" ] || [ ! -d "$REPO_DIR" ]; then
  echo "[$(timestamp)] Missing US_INTRADAY_REPO_DIR or repo directory not found."
  exit 0
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "[$(timestamp)] Python environment not found: $PYTHON_BIN"
  echo "[$(timestamp)] Run scripts/install_us_intraday_local_launchd.sh again."
  exit 0
fi

acquire_lock

OPENAI_API_KEY="$(read_secret OPENAI_API_KEY)"
TELEGRAM_BOT_TOKEN="$(read_secret TELEGRAM_BOT_TOKEN)"
TELEGRAM_CHAT_ID="$(read_secret TELEGRAM_CHAT_ID)"

if [ -z "$OPENAI_API_KEY" ] || [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
  echo "[$(timestamp)] Missing Keychain secrets. Run scripts/configure_us_intraday_keychain.sh."
  exit 0
fi

export OPENAI_API_KEY
export TELEGRAM_BOT_TOKEN
export TELEGRAM_CHAT_ID
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-5.4-mini}"
export STOCK_LIST="${STOCK_LIST:-NVDA,MSFT,AAPL,AMZN,GOOGL,META,TSLA,AMD,AVGO,TSM,PLTR,JPM,V,LLY,COST,SPY,QQQ,SMH,TLT,GLD,BABA,CRWV,OKLO,SNDK,VRT,SPX,NASDAQ,VIX,IWM,XLK,XLF,XLE,HYG,UUP}"
export PORTFOLIO_STOCK_LIST="${PORTFOLIO_STOCK_LIST:-AVGO,BABA,CRWV,OKLO,PLTR,QQQ,SNDK,TSM,VRT}"
export REPORT_LANGUAGE="${REPORT_LANGUAGE:-zh}"
export MARKET_REVIEW_REGION="${MARKET_REVIEW_REGION:-us}"
export US_INTRADAY_RADAR_ENABLED="${US_INTRADAY_RADAR_ENABLED:-true}"
export US_INTRADAY_WINDOWS="${US_INTRADAY_WINDOWS:-pre_open,open_15,open_30,open_60,midday,power_hour,close_15}"
export US_INTRADAY_PUSH_NIGHT="${US_INTRADAY_PUSH_NIGHT:-true}"
export US_INTRADAY_ALERT_HOLDING_CHANGE_PCT="${US_INTRADAY_ALERT_HOLDING_CHANGE_PCT:-2.5}"
export US_INTRADAY_ALERT_INDEX_CHANGE_PCT="${US_INTRADAY_ALERT_INDEX_CHANGE_PCT:-1.0}"
export US_INTRADAY_ALERT_VIX_CHANGE_PCT="${US_INTRADAY_ALERT_VIX_CHANGE_PCT:-5.0}"
export US_INTRADAY_OPPORTUNITY_MAX="${US_INTRADAY_OPPORTUNITY_MAX:-5}"
export US_INTRADAY_REPORT_LANGUAGE="${US_INTRADAY_REPORT_LANGUAGE:-zh}"
export US_INTRADAY_WINDOW_TOLERANCE_MINUTES="${US_INTRADAY_WINDOW_TOLERANCE_MINUTES:-18}"
export US_INTRADAY_CATCHUP_MINUTES="${US_INTRADAY_CATCHUP_MINUTES:-45}"
export US_INTRADAY_CLOSE_CATCHUP_MINUTES="${US_INTRADAY_CLOSE_CATCHUP_MINUTES:-120}"
export US_INTRADAY_READABLE_REPORT="${US_INTRADAY_READABLE_REPORT:-true}"
export US_INTRADAY_JARGON_LEVEL="${US_INTRADAY_JARGON_LEVEL:-explained}"
export US_INTRADAY_MAX_ACTION_ITEMS="${US_INTRADAY_MAX_ACTION_ITEMS:-5}"
export US_INTRADAY_SHOW_TECHNICAL_DETAILS="${US_INTRADAY_SHOW_TECHNICAL_DETAILS:-false}"
export US_INTRADAY_DEDUPE_ENABLED="${US_INTRADAY_DEDUPE_ENABLED:-true}"
export US_INTRADAY_DEDUPE_LOOKBACK_HOURS="${US_INTRADAY_DEDUPE_LOOKBACK_HOURS:-24}"
export US_INTRADAY_REQUIRE_FRESH_QUOTES="${US_INTRADAY_REQUIRE_FRESH_QUOTES:-true}"
export US_INTRADAY_QUOTE_FRESHNESS_MINUTES="${US_INTRADAY_QUOTE_FRESHNESS_MINUTES:-20}"
export US_INTRADAY_PRE_OPEN_FAST_MODE="${US_INTRADAY_PRE_OPEN_FAST_MODE:-true}"
export US_COMMANDER_ENABLED="${US_COMMANDER_ENABLED:-true}"
export US_COMMANDER_MODE="${US_COMMANDER_MODE:-swing_intraday}"
export US_COMMANDER_RISK_STYLE="${US_COMMANDER_RISK_STYLE:-balanced}"
export US_COMMANDER_LLM_MODE="${US_COMMANDER_LLM_MODE:-triggered}"
export US_COMMANDER_MAX_ACTIONS="${US_COMMANDER_MAX_ACTIONS:-5}"
export US_COMMANDER_MAX_OPPORTUNITIES="${US_COMMANDER_MAX_OPPORTUNITIES:-3}"
export US_COMMANDER_MIN_ALERT_SCORE="${US_COMMANDER_MIN_ALERT_SCORE:-70}"
export US_COMMANDER_MEMORY_ENABLED="${US_COMMANDER_MEMORY_ENABLED:-true}"
export US_COMMANDER_LANGUAGE_STYLE="${US_COMMANDER_LANGUAGE_STYLE:-plain_with_terms}"
export US_COMMANDER_SHOW_TERM_EXPLANATIONS="${US_COMMANDER_SHOW_TERM_EXPLANATIONS:-true}"
export US_COMMANDER_MAX_LEARNING_NOTES="${US_COMMANDER_MAX_LEARNING_NOTES:-3}"
export US_COMMANDER_OPTIONS_ENABLED="${US_COMMANDER_OPTIONS_ENABLED:-true}"
export US_COMMANDER_OPTION_MIN_DTE="${US_COMMANDER_OPTION_MIN_DTE:-14}"
export US_COMMANDER_OPTION_MAX_DTE="${US_COMMANDER_OPTION_MAX_DTE:-45}"
export US_COMMANDER_OPTION_MAX_RISK_PCT="${US_COMMANDER_OPTION_MAX_RISK_PCT:-1.0}"
export US_COMMANDER_DIRECTNESS="${US_COMMANDER_DIRECTNESS:-aggressive}"
export US_COMMANDER_POSITION_SIZING="${US_COMMANDER_POSITION_SIZING:-relative}"
export US_COMMANDER_CARD_STYLE="${US_COMMANDER_CARD_STYLE:-command_first}"
export US_COMMANDER_BRIEF_MODE="${US_COMMANDER_BRIEF_MODE:-true}"
export US_COMMANDER_BRIEF_MAX_LINES="${US_COMMANDER_BRIEF_MAX_LINES:-8}"
export US_COMMANDER_VISUAL_CARD="${US_COMMANDER_VISUAL_CARD:-false}"
export US_COMMANDER_TERM_GLOSSARY_MODE="${US_COMMANDER_TERM_GLOSSARY_MODE:-footer}"
export US_COMMANDER_MAX_GLOSSARY_TERMS="${US_COMMANDER_MAX_GLOSSARY_TERMS:-2}"
export US_COMMANDER_MEMORY_DIR="${US_COMMANDER_MEMORY_DIR:-$APP_SUPPORT_DIR/commander-state}"
export MARKDOWN_TO_IMAGE_CHANNELS="${MARKDOWN_TO_IMAGE_CHANNELS:-}"
export MD2IMG_ENGINE="${MD2IMG_ENGINE:-wkhtmltoimage}"
export US_INTRADAY_LOCAL_MODE="true"
export US_INTRADAY_LOCAL_MARKER_DIR="${US_INTRADAY_LOCAL_MARKER_DIR:-$APP_SUPPORT_DIR/markers}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export REALTIME_SOURCE_PRIORITY="${REALTIME_SOURCE_PRIORITY:-yfinance,tencent,akshare_sina,efinance,akshare_em}"

mkdir -p "$APP_SUPPORT_DIR/markers"
cd "$REPO_DIR"

force_arg=()
if [ "$FORCE_RUN" = "true" ]; then
  force_arg=(--force-run)
fi

echo "[$(timestamp)] Running local US intraday radar window=$WINDOW force_run=$FORCE_RUN"
"$PYTHON_BIN" main.py --intraday-radar --intraday-window "$WINDOW" "${force_arg[@]}"
