#!/bin/bash
# Install (or reinstall) the capability-queue watcher as a launchd agent.
# Idempotent — safe to re-run after a machine change, a repo move, or a Python upgrade.
set -euo pipefail

PLIST_NAME="com.secondbrain.capabilitywatcher.plist"
SRC="$HOME/second-brain/scripts/$PLIST_NAME"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

PY=$(awk -F'[<>]' '/Versions.*bin\/python3/{print $3; exit}' "$SRC")
if [ ! -x "$PY" ]; then
    echo "ERROR: python3 at '$PY' (from $PLIST_NAME) is missing."
    echo "       Update the plist's first ProgramArguments entry to: $(command -v python3)"
    exit 1
fi
"$PY" -c "import dotenv, supabase" 2>/dev/null || {
    echo "ERROR: '$PY' can't import dotenv/supabase — the watcher would fail every run."
    echo "       Install them for THAT interpreter: $PY -m pip install python-dotenv supabase"
    exit 1
}
[ -x "$HOME/.local/bin/claude" ] || echo "WARNING: ~/.local/bin/claude not found — builds can't spawn."

cp "$SRC" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Loaded. Status (PID / last exit / label):"
launchctl list | grep capabilitywatcher || echo "  NOT LISTED — load failed"
sleep 3
echo "Heartbeat: $(cat "$HOME/second-brain/.capability_watcher_heartbeat" 2>/dev/null || echo 'NOT WRITTEN YET')"
echo
echo "Verify anytime:  python3 ~/second-brain/scripts/capability_watcher.py --dry-run"
echo "Uninstall:       launchctl unload $DEST && rm $DEST"
