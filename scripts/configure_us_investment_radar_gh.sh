#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Configure GitHub Actions for the US investment radar.

Usage:
  scripts/configure_us_investment_radar_gh.sh owner/repo

Prerequisites:
  - Install and authenticate GitHub CLI: gh auth login
  - Run this against your fork, not the upstream repository.

The script writes non-sensitive settings as repository Variables and prompts
for sensitive values as repository Secrets.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo="${1:-}"
if [[ -z "$repo" ]]; then
  echo "Missing repository, for example: your-github-name/daily_stock_analysis" >&2
  usage >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI 'gh' is required. Install it first, then run: gh auth login" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated. Run: gh auth login" >&2
  exit 1
fi

stock_list="NVDA,MSFT,AAPL,AMZN,GOOGL,META,TSLA,AMD,AVGO,TSM,PLTR,JPM,V,LLY,COST,SPY,QQQ,SMH,TLT,GLD,BABA,CRWV,OKLO,SNDK,VRT,SPX,NASDAQ,VIX,IWM,XLK,XLF,XLE,HYG,UUP"
portfolio_stock_list="AVGO,BABA,CRWV,OKLO,PLTR,QQQ,SNDK,TSM,VRT"

echo "Configuring repository Variables on $repo..."
gh variable set OPENAI_MODEL --repo "$repo" --body "gpt-5.4-mini"
gh variable set REPORT_LANGUAGE --repo "$repo" --body "zh"
gh variable set MARKET_REVIEW_REGION --repo "$repo" --body "us"
gh variable set MARKET_REVIEW_ENABLED --repo "$repo" --body "true"
gh variable set REPORT_TYPE --repo "$repo" --body "full"
gh variable set REPORT_PROFILE --repo "$repo" --body "us_investment_radar"
gh variable set REPORT_SUMMARY_ONLY --repo "$repo" --body "false"
gh variable set SINGLE_STOCK_NOTIFY --repo "$repo" --body "false"
gh variable set PORTFOLIO_STOCK_LIST --repo "$repo" --body "$portfolio_stock_list"
gh variable set OPPORTUNITY_MAX --repo "$repo" --body "8"
gh variable set RISK_WATCH_MAX --repo "$repo" --body "8"
gh variable set US_INTRADAY_RADAR_ENABLED --repo "$repo" --body "true"
gh variable set US_INTRADAY_WINDOWS --repo "$repo" --body "pre_open,open_15,open_30,open_60,midday,power_hour,close_15"
gh variable set US_INTRADAY_PUSH_NIGHT --repo "$repo" --body "true"
gh variable set US_INTRADAY_ALERT_HOLDING_CHANGE_PCT --repo "$repo" --body "2.5"
gh variable set US_INTRADAY_ALERT_INDEX_CHANGE_PCT --repo "$repo" --body "1.0"
gh variable set US_INTRADAY_ALERT_VIX_CHANGE_PCT --repo "$repo" --body "5.0"
gh variable set US_INTRADAY_OPPORTUNITY_MAX --repo "$repo" --body "5"
gh variable set US_INTRADAY_REPORT_LANGUAGE --repo "$repo" --body "zh"
gh variable set US_INTRADAY_WINDOW_TOLERANCE_MINUTES --repo "$repo" --body "18"
gh variable set US_INTRADAY_READABLE_REPORT --repo "$repo" --body "true"
gh variable set US_INTRADAY_JARGON_LEVEL --repo "$repo" --body "explained"
gh variable set US_INTRADAY_MAX_ACTION_ITEMS --repo "$repo" --body "5"
gh variable set US_INTRADAY_SHOW_TECHNICAL_DETAILS --repo "$repo" --body "false"
gh variable set US_INTRADAY_DEDUPE_ENABLED --repo "$repo" --body "true"
gh variable set US_INTRADAY_DEDUPE_LOOKBACK_HOURS --repo "$repo" --body "24"
gh variable set MAX_WORKERS --repo "$repo" --body "1"
gh variable set ANALYSIS_DELAY --repo "$repo" --body "5"
gh variable set STOCK_LIST --repo "$repo" --body "$stock_list"

echo
echo "Now enter repository Secrets. Input is hidden where possible."
read -rsp "OPENAI_API_KEY: " openai_api_key
echo
read -rsp "TELEGRAM_BOT_TOKEN: " telegram_bot_token
echo
read -rp "TELEGRAM_CHAT_ID: " telegram_chat_id

if [[ -z "$openai_api_key" || -z "$telegram_bot_token" || -z "$telegram_chat_id" ]]; then
  echo "OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID are required." >&2
  exit 1
fi

printf '%s' "$openai_api_key" | gh secret set OPENAI_API_KEY --repo "$repo"
printf '%s' "$telegram_bot_token" | gh secret set TELEGRAM_BOT_TOKEN --repo "$repo"
printf '%s' "$telegram_chat_id" | gh secret set TELEGRAM_CHAT_ID --repo "$repo"

echo
read -rp "Optional TAVILY_API_KEYS (press Enter to skip): " tavily_api_keys
if [[ -n "$tavily_api_keys" ]]; then
  printf '%s' "$tavily_api_keys" | gh secret set TAVILY_API_KEYS --repo "$repo"
fi

read -rp "Optional BRAVE_API_KEYS (press Enter to skip): " brave_api_keys
if [[ -n "$brave_api_keys" ]]; then
  printf '%s' "$brave_api_keys" | gh secret set BRAVE_API_KEYS --repo "$repo"
fi

echo
echo "Done. To run the first analysis:"
echo "  gh workflow run daily_analysis.yml --repo $repo -f mode=full -f force_run=true"
echo "  gh workflow run us_intraday_radar.yml --repo $repo -f window=open_15 -f force_run=true"
echo
echo "To watch it:"
echo "  gh run list --repo $repo --workflow daily_analysis.yml --limit 3"
echo "  gh run list --repo $repo --workflow us_intraday_radar.yml --limit 3"
