#!/bin/zsh
set -euo pipefail

SERVICE_PREFIX="${US_INTRADAY_KEYCHAIN_PREFIX:-us-intraday-radar}"
ACCOUNT="${US_INTRADAY_KEYCHAIN_ACCOUNT:-$USER}"

read_hidden() {
  local prompt="$1"
  local value
  printf "%s" "$prompt" > /dev/tty
  stty -echo
  IFS= read -r value < /dev/tty
  stty echo
  printf "\n" > /dev/tty
  printf "%s" "$value"
}

save_secret() {
  local key="$1"
  local value="$2"
  local service="$SERVICE_PREFIX.$key"
  security add-generic-password \
    -a "$ACCOUNT" \
    -s "$service" \
    -w "$value" \
    -U >/dev/null
  echo "Saved $key to macOS Keychain service $service"
}

openai_key="$(read_hidden 'OPENAI_API_KEY: ')"
telegram_token="$(read_hidden 'TELEGRAM_BOT_TOKEN: ')"
telegram_chat_id="$(read_hidden 'TELEGRAM_CHAT_ID: ')"

if [ -z "$openai_key" ] || [ -z "$telegram_token" ] || [ -z "$telegram_chat_id" ]; then
  echo "All three values are required."
  exit 1
fi

save_secret "OPENAI_API_KEY" "$openai_key"
save_secret "TELEGRAM_BOT_TOKEN" "$telegram_token"
save_secret "TELEGRAM_CHAT_ID" "$telegram_chat_id"

echo "US intraday radar secrets are now stored in macOS Keychain."
