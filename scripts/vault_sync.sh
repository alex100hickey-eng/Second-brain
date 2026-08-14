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
# The git DIR already lives outside iCloud (~/.second-brain-vault.git since 2026-07-19;
# the vault's .git is a 52-byte pointer file). iCloud can still evict the pointer file
# or any CONTENT file (dataless flag) — both are handled below. This script never
# modifies vault CONTENT; it only downloads, then add/commit/pushes what's already there.

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

# iCloud also EVICTS file CONTENT (APFS "dataless" flag) — git then fails reading the
# file with EDEADLK ("Resource deadlock avoided"), observed 2026-07-22 on Schedule/
# brief-2026-07-22.md. Materialize any evicted files (including the .git pointer file)
# before touching git; brctl only downloads content, it never modifies it.
DATALESS_COUNT=$(find . -type f -flags +dataless 2>/dev/null | wc -l | tr -d ' ')
if [ "$DATALESS_COUNT" -gt 0 ]; then
    echo "[$(ts)] MATERIALIZING — $DATALESS_COUNT iCloud-evicted file(s), requesting download."
    find . -type f -flags +dataless 2>/dev/null | while IFS= read -r f; do
        brctl download "$f" >/dev/null 2>&1 || true
    done
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do   # wait up to ~60s for iCloud
        sleep 5
        DATALESS_COUNT=$(find . -type f -flags +dataless 2>/dev/null | wc -l | tr -d ' ')
        [ "$DATALESS_COUNT" -eq 0 ] && break
    done
    if [ "$DATALESS_COUNT" -gt 0 ]; then
        echo "[$(ts)] ERROR — $DATALESS_COUNT evicted file(s) still dataless after 60s; skipping this run."
        report error "iCloud-evicted vault files failed to materialize" "$DATALESS_COUNT still dataless"
        exit 1
    fi
    echo "[$(ts)] MATERIALIZED — all evicted files downloaded; proceeding."
fi

# Detect the iCloud .git eviction explicitly, before any mutating command.
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "[$(ts)] ERROR — .git unreadable (iCloud eviction). Vault is NOT syncing to GitHub."
    report error "vault .git unreadable — iCloud evicted it; vault not syncing" "$VAULT/.git"
    exit 1
fi

# Pull BEFORE committing. This box used to be the only writer, so pushing blind was
# safe. It isn't any more: CLARVIS also writes notes on the SERVER (managed research
# tasks land in its /data/vault), and the server's sync only ever ran `git pull`, so
# those files had nowhere to go. On 2026-08-14 four of them — 42 KB of council-reviewed
# money research — were found stranded on the server, invisible to Obsidian and to
# GitHub, with no idea how long they'd been there.
#
# --rebase keeps history linear; --autostash protects any in-flight local edit. A pull
# failure is NOT fatal: pushing local work still beats skipping the run, and the
# server's own push (if it lands first) will simply make the next push a no-fast-forward
# that this script reports rather than hides.
if ! git pull --rebase --autostash 2>&1; then
    echo "[$(ts)] WARN — git pull failed; continuing with local commit/push."
    report warning "vault pull failed (continuing)" "$VAULT"
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
