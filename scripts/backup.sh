#!/usr/bin/env bash
#
# backup.sh — timestamped snapshot of the Second Brain project.
#
# Creates a zip in ~/second-brain-backups/ containing the project source, the
# conversation-memory DB, task/goal data, run drafts, and notes — but NOT the heavy,
# regenerable artifacts (Whisper model weights, generated media, video scratch). Also
# takes a separate READ-ONLY snapshot of the Obsidian vault. Keeps the 7 most recent of
# each. Run it by hand, or ask Jarvis to "back up my system". It does NOT schedule itself
# — see the action list for how to add a cron/launchd job if you want it automatic.
#
# Safe: read-only on everything it copies; never deletes anything except its own old
# backups beyond the 7 most recent.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${HOME}/second-brain-backups"
KEEP=7
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

# --- Load OBSIDIAN_VAULT_PATH from .env if present (for the vault snapshot) ---
VAULT_PATH=""
if [ -f "${PROJECT_DIR}/.env" ]; then
  VAULT_PATH="$(grep -E '^OBSIDIAN_VAULT_PATH=' "${PROJECT_DIR}/.env" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
fi
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-$VAULT_PATH}"
# Fall back to the app's default real Obsidian vault location (read-only) if unset.
if [ -z "$VAULT_PATH" ]; then
  VAULT_PATH="${HOME}/Library/Mobile Documents/iCloud~md~obsidian/Documents/Second brain"
fi

# ---------------------------------------------------------------------------
# 1. Project snapshot (excludes heavy / regenerable artifacts; INCLUDES the
#    conversation DB and task/goal DBs, which are the irreplaceable state).
# ---------------------------------------------------------------------------
PROJECT_ZIP="${BACKUP_DIR}/second-brain-${STAMP}.zip"
echo "Backing up project → ${PROJECT_ZIP}"

# zip from inside the project so paths are relative. -x excludes patterns.
( cd "$PROJECT_DIR" && zip -r -q "$PROJECT_ZIP" . \
    -x '*.git/*' \
    -x '__pycache__/*' -x '*/__pycache__/*' -x '*.pyc' \
    -x 'models/*' \
    -x 'media_lib/*' \
    -x 'video_work/*' \
    -x 'inbox/*' \
    -x 'screenshots/*' \
    -x '*.DS_Store' \
) || { echo "Project zip failed"; exit 1; }

PROJECT_SIZE="$(du -h "$PROJECT_ZIP" | cut -f1)"
echo "  project snapshot: ${PROJECT_SIZE}"

# ---------------------------------------------------------------------------
# 2. Obsidian vault snapshot (READ-ONLY copy out — never writes to the vault).
# ---------------------------------------------------------------------------
if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then
  VAULT_ZIP="${BACKUP_DIR}/obsidian-vault-${STAMP}.zip"
  echo "Backing up Obsidian vault (read-only) → ${VAULT_ZIP}"
  ( cd "$VAULT_PATH" && zip -r -q "$VAULT_ZIP" . -x '*.obsidian/workspace*' -x '*.DS_Store' ) \
    && echo "  vault snapshot: $(du -h "$VAULT_ZIP" | cut -f1)" \
    || echo "  (vault snapshot skipped — zip error)"
else
  echo "No readable Obsidian vault found (OBSIDIAN_VAULT_PATH unset or missing) — skipping vault snapshot."
fi

# ---------------------------------------------------------------------------
# 3. Retention — keep only the newest $KEEP of each kind.
# ---------------------------------------------------------------------------
prune() {
  local pattern="$1"
  # List newest-first, skip the first $KEEP, delete the rest.
  ls -1t ${BACKUP_DIR}/${pattern} 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "  pruning old backup: $(basename "$old")"
    rm -f "$old"
  done
}
prune 'second-brain-*.zip'
prune 'obsidian-vault-*.zip'

COUNT="$(ls -1 ${BACKUP_DIR}/second-brain-*.zip 2>/dev/null | wc -l | tr -d ' ')"
echo "Done. ${COUNT} project snapshot(s) retained in ${BACKUP_DIR} (keeping newest ${KEEP})."
