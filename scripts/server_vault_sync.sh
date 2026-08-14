#!/bin/sh
# server_vault_sync.sh — the SERVER half of vault sync, run every 10 min by the
# Coolify scheduled task `sync-vault` on the second-brain app.
#
# Why this is a file and not a one-liner in the Coolify task field:
#
#   1. Coolify mangles it. The previous inline command used `$(git status --porcelain)`,
#      which the HOST shell expanded to empty before the container ever saw it, so the
#      commit branch silently never ran — while Coolify reported "Success" in 0s. The
#      same command pasted into `docker exec` by hand worked first try. Quoting and
#      expansion through that field cannot be trusted.
#   2. A command that lives only in a Coolify text box is invisible code. On 2026-08-14
#      a whole second app (`money-clips-agent`) ran daily for weeks because nothing
#      about it existed in this repo. Anything that runs on a schedule belongs in git.
#
# What it fixes: sync used to be `git pull` ONLY. CLARVIS also WRITES to the vault on
# the server (managed research tasks), and those files had nowhere to go — four of
# them, 42 KB of council-reviewed money research, sat stranded and invisible until
# 2026-08-14. This makes the server a full peer: pull, then push whatever it wrote.
#
# The Mac half (scripts/vault_sync.sh) now pulls before pushing, so both sides can
# write without clobbering each other.

set -u

VAULT=/data/vault
REPO_URL="https://x-access-token:${VAULT_GIT_TOKEN}@github.com/alex100hickey-eng/second-brain-vault.git"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ ! -d "$VAULT/.git" ]; then
    echo "[$(ts)] CLONING — no vault present at $VAULT"
    git clone "$REPO_URL" "$VAULT" || { echo "[$(ts)] ERROR — clone failed"; exit 1; }
    echo "[$(ts)] CLONED"
    exit 0
fi

cd "$VAULT" || { echo "[$(ts)] ERROR — cannot cd to $VAULT"; exit 1; }

# Identity for commits made by the server. Set every run so a fresh clone still works.
git config user.email "clarvis@splitframestudio.com"
git config user.name "CLARVIS"

# Pull first so the server never pushes on top of Alex's Mac.
if ! git pull --rebase --autostash 2>&1; then
    echo "[$(ts)] WARN — pull failed; still attempting to push local writes."
fi

git add -A || { echo "[$(ts)] ERROR — git add failed"; exit 1; }

if git diff --cached --quiet; then
    echo "[$(ts)] IDLE — nothing written server-side (healthy)."
    exit 0
fi

# Name the files being rescued: the failure mode this replaces was silent, so the log
# should make server-originated writes obvious.
echo "[$(ts)] PUSHING — server-side changes:"
git diff --cached --name-only | sed 's/^/    /'

if ! git commit -m "Server auto-sync $(ts)" 2>&1; then
    echo "[$(ts)] ERROR — commit failed"
    exit 1
fi

if ! git push 2>&1; then
    echo "[$(ts)] ERROR — push failed; vault is now AHEAD of GitHub."
    exit 1
fi

echo "[$(ts)] SYNCED — server writes pushed to GitHub."
