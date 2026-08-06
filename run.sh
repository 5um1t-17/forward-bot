#!/usr/bin/env bash
# Start the Telegram Message Transfer Bot.
# Creates a local .env from the template if missing.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "Created .env from template. Edit it and set API_ID, API_HASH and BOT_TOKEN."
    else
        echo "Missing .env and .env.example. Create a .env file with API_ID, API_HASH and BOT_TOKEN."
        exit 1
    fi
fi

exec python3 -m bot.main
