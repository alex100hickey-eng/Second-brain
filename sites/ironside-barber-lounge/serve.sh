#!/usr/bin/env bash
# One-command local preview for "ironside-barber-lounge". Serves this folder on localhost:8080.
# (Deliberately NOT port 5001 — that's the chat app.)
cd "$(dirname "$0")" || exit 1
PORT="${1:-8080}"
echo "Serving ironside-barber-lounge at http://localhost:$PORT  (Ctrl+C to stop)"
python3 -m http.server "$PORT"
