#!/bin/zsh
set -euo pipefail

SERVICE_PREFIX="${US_INTRADAY_KEYCHAIN_PREFIX:-us-intraday-radar}"
ACCOUNT="${US_INTRADAY_KEYCHAIN_ACCOUNT:-$USER}"
TOKEN="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE_PREFIX.TELEGRAM_BOT_TOKEN" -w)"

python3 - "$TOKEN" <<'PY'
import json
import sys
import urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/getUpdates"
with urllib.request.urlopen(url, timeout=15) as response:
    data = json.load(response)

print("ok=", data.get("ok"))
seen = set()
for item in data.get("result", [])[-20:]:
    msg = item.get("message") or item.get("edited_message") or item.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or chat_id in seen:
        continue
    seen.add(chat_id)
    title = chat.get("title") or chat.get("username") or " ".join(
        part for part in [chat.get("first_name"), chat.get("last_name")] if part
    )
    print(f"chat_id={chat_id} type={chat.get('type')} title={title}")

if not seen:
    print("没有看到 chat_id。请先在 Telegram 里给机器人发送 /start 或 test，然后再运行一次。")
PY
