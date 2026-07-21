#!/bin/bash
# Auto-commits and pushes the Obsidian vault to second-brain-vault on GitHub.
# Run every 10 min by ~/Library/LaunchAgents/com.secondbrain.vaultsync.plist.
#
# The vault's .git lives on iCloud Drive, which intermittently EVICTS it — git then
# fails with `fatal: error reading .../.git` and the vault silently stops reaching
# GitHub until iCloud re-materializes it (a ~4h silent outage was observed 2026-07-20;
# audit finding #4). This script now:
#   - stamps every run with a timestamp + a clear status (IDLE / SYNCED / ERROR), so a
#     healthy do-nothing run is distinguishable from a broken one in the log; and
#   - reports git failures to the monitor (system_event row) so the outage surfaces in
#     CLARVIS's incident log / dashboard instead of dying quietly in this file.
# The deeper fix (move .git out of iCloud via --separate-git-dir) is a documented,
# Alex-run migration — see the handoff / OVERNIGHT_REPORT.md. This script never
# modifies vault CONTENT; it only add/commit/pushes what's already there.

set -uo pipefail

VAULT="${VAULT_SYNC_PATH:-/Users/alexhickey24/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use the framework Python (it has supabase/dotenv); /usr/bin/python3 does not.
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
[ -x "$PY" ] || PY="python3"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
report() {  # report <level> <message> [detail]  — fail-soft, never blocks the job
    "$PY" "$SCRIPT_DIR/report_event.py" "vault-sync" "$1" "$2" "${3:-}" >/dev/null 2>&1 || true
}

if ! cd "$VAULT" 2>/dev/null; then
    echo "[$(ts)] ERROR — vault path unavailable (iCloud not mounted?): $VAULT"
    report error "vault path unavailable" "$VAULT"
    exit 1
fi

# Detect the iCloud .git eviction explicitly, before any mutating command.
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "[$(ts)] ERROR — .git unreadable (iCloud eviction). Vault is NOT syncing to GitHub."
    report error "vault .git unreadable — iCloud evicted it; vault not syncing" "$VAULT/.git"
    exit 1
fi

if ! git add -A 2>&1; then
    echo "[$(ts)] ERROR — git add failed."
    report error "git add failed" "$VAULT"
    exit 1
fi

if git diff --cached --quiet; then
    echo "[$(ts)] IDLE — no vault changes to sync (healthy)."
    exit 0
fi

if ! git commit -m "Auto-sync $(date -u +"%Y-%m-%dT%H:%M:%SZ")" 2>&1; then
    echo "[$(ts)] ERROR — git commit failed."
    report error "git commit failed" "$VAULT"
    exit 1
fi

if ! git push origin main 2>&1; then
    echo "[$(ts)] ERROR — git push failed; vault is now AHEAD of GitHub."
    report error "git push failed — vault ahead of GitHub" "$VAULT"
    exit 1
fi

echo "[$(ts)] SYNCED — vault changes committed and pushed to GitHub."
