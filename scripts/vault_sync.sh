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
PY="${VAULT_SYNC_PY:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
[ -x "$PY" ] || PY="python3"
BRCTL="${VAULT_SYNC_BRCTL:-brctl}"          # tests pass /usr/bin/true
WAIT_TRIES="${VAULT_SYNC_WAIT_TRIES:-12}"   # 5 s each

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
report() {  # report <level> <message> [detail]  — fail-soft, never blocks the job
    "$PY" "$SCRIPT_DIR/report_event.py" "vault-sync" "$1" "$2" "${3:-}" >/dev/null 2>&1 || true
}
# Heartbeat on a HEALTHY finish only. A job that stops firing entirely — an
# unloaded plist, a Mac asleep for a day — is invisible otherwise: no error is
# logged because no code runs. Stale-after 2h on a 10-min cadence tolerates
# normal sleep without crying wolf. Never beat on a failure path, or a
# permanently broken sync reads as healthy.
beat() {  # beat <note>
    "$PY" "$SCRIPT_DIR/beat.py" "vault-sync-mac" 7200 "${1:-ok}" >/dev/null 2>&1 || true
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
# Claude/ is skipped: it is git-ignored (a local mirror of Claude Code's memory, written by
# ~/.claude/obsidian-mirror/mirror.py), so git never reads it and an evicted copy there
# must not fail this run.
dataless() {
    if [ -n "${VAULT_SYNC_FAKE_DATALESS:-}" ]; then printf '%s\n' "$VAULT_SYNC_FAKE_DATALESS"; return; fi
    find . -type f -flags +dataless -not -path './Claude/*' 2>/dev/null
}
LEFT_OUT=()  # files still dataless after the wait: left out of this run
SKIPPED=()   # tracked files still dataless after the wait: git told not to read them (below)
clear_skips() {
    [ ${#SKIPPED[@]} -gt 0 ] || return 0
    git update-index --no-assume-unchanged -- "${SKIPPED[@]}" >/dev/null 2>&1 || true
}
trap clear_skips EXIT
DATALESS_COUNT=$(dataless | wc -l | tr -d ' ')
if [ "$DATALESS_COUNT" -gt 0 ]; then
    echo "[$(ts)] MATERIALIZING — $DATALESS_COUNT iCloud-evicted file(s), requesting download."
    dataless | while IFS= read -r f; do
        "$BRCTL" download "$f" >/dev/null 2>&1 || true
    done
    i=0
    while [ "$i" -lt "$WAIT_TRIES" ]; do   # wait up to ~60s for iCloud
        i=$((i + 1))
        [ -n "${VAULT_SYNC_FAKE_DATALESS:-}" ] || sleep 5
        DATALESS_COUNT=$(dataless | wc -l | tr -d ' ')
        [ "$DATALESS_COUNT" -eq 0 ] && break
    done
    if [ "$DATALESS_COUNT" -gt 0 ]; then
        # This used to skip the WHOLE run. On 2026-09-24 seven files iCloud would not give
        # back (they read empty; git: "short read while indexing") stopped every sync from
        # 12:19 on, so nothing written after that reached git, and everything that reads the
        # vault's git copy as a fallback (the send gate, reply watch, the 07:30 static-first
        # backstop) read a copy twelve hours old. A dataless file has no local edit to lose:
        # iCloud only evicts content that is safely in the cloud. So leave just those files
        # out of this run and sync everything else. The .git pointer file is the exception:
        # without it there is no repo, so that still stops the run.
        while IFS= read -r f; do
            [ -n "$f" ] && LEFT_OUT+=("${f#./}")
        done < <(dataless)
        for f in "${LEFT_OUT[@]}"; do
            if [ "$f" = ".git" ]; then
                echo "[$(ts)] ERROR — the .git pointer file is still dataless after the wait; skipping this run."
                report error "vault .git pointer evicted and not materializing" "$VAULT/.git"
                exit 1
            fi
        done
        echo "[$(ts)] PARTIAL — $DATALESS_COUNT evicted file(s) still dataless after the wait; syncing everything else, leaving out: ${LEFT_OUT[*]}"
        report warning "iCloud-evicted vault files left out of this sync" "$DATALESS_COUNT still dataless: ${LEFT_OUT[*]}"
    else
        echo "[$(ts)] MATERIALIZED — all evicted files downloaded; proceeding."
    fi
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
# Files left out above: excluded from `git add`, and the tracked ones marked assume-unchanged
# so git does not read them at all this run. Without the mark, a tracked file whose stat info
# changed on eviction is re-read by the index refresh in stash/commit, and the short read fails
# the whole commit even with the file excluded from `git add` (seen 2026-09-25). The trap
# clears the mark on exit, so a later real edit to the file is picked up as usual.
EXCLUDES=()
for f in ${LEFT_OUT[@]+"${LEFT_OUT[@]}"}; do
    EXCLUDES+=(":(exclude)$f")
    git ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && SKIPPED+=("$f")
done
if [ ${#SKIPPED[@]} -gt 0 ]; then
    git update-index --assume-unchanged -- "${SKIPPED[@]}" >/dev/null 2>&1 || true
fi

if ! git pull --rebase --autostash 2>&1; then
    echo "[$(ts)] WARN — git pull failed; continuing with local commit/push."
    report warning "vault pull failed (continuing)" "$VAULT"
fi

if ! git add -A -- . ${EXCLUDES[@]+"${EXCLUDES[@]}"} 2>&1; then
    echo "[$(ts)] ERROR — git add failed."
    report error "git add failed" "$VAULT"
    exit 1
fi

if git diff --cached --quiet; then
    echo "[$(ts)] IDLE — no vault changes to sync (healthy)."
    beat "idle — no changes"
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
if [ ${#EXCLUDES[@]} -gt 0 ]; then beat "synced (${#EXCLUDES[@]} dataless left out)"; else beat "synced"; fi
