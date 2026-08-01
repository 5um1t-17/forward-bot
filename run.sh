#!/usr/bin/env bash
# Start the Telegram Message Transfer Bot.
# Creates a local .env from the template if missing.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from template. Edit it and set API_ID, API_HASH and BOT_TOKEN."
fi

exec python3 -m bot.main
