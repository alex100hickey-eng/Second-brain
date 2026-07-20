#!/usr/bin/env bash
#
# jarvis-launch.sh — pick an approved drafted run and get its exact launch command.
#
# Jarvis DRAFTS overnight-build runs (run_drafter.py) but never launches them. This
# helper is the human side of that line: it lists your APPROVED drafts, lets you pick
# one, prints the exact command YOU would run to launch it, and copies the draft's path
# to your clipboard.
#
#   *** THIS SCRIPT NEVER INVOKES claude. ***
#   It only prints a command and copies a path. Launching is your deliberate action,
#   done by pasting/running the printed command yourself. Even after you confirm, this
#   script does nothing but print and copy.
#
# Usage:  bash jarvis-launch.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX="${PROJECT_DIR}/run_drafts/index.json"
PY="$(command -v python3 || command -v python)"

if [ ! -f "$INDEX" ]; then
  echo "No drafted runs yet ($INDEX not found). Ask Jarvis to \"draft a run to <goal>\" first."
  exit 0
fi

echo "=============================================="
echo " Jarvis — launch an APPROVED drafted run"
echo " (this script only prints & copies — it never launches anything)"
echo "=============================================="
echo

# List approved drafts (id, title, file) via python for robust JSON parsing.
# Read into an array WITHOUT mapfile (macOS ships bash 3.2, which lacks it).
ROWS=()
while IFS= read -r line; do
  [ -n "$line" ] && ROWS+=("$line")
done < <("$PY" - "$INDEX" <<'PYEOF'
import json, sys
try:
    entries = json.load(open(sys.argv[1]))
except Exception:
    entries = []
approved = [e for e in entries if e.get("status") == "approved"]
for e in approved:
    print(f"{e['id']}\t{e.get('title','(untitled)')}\t{e.get('file','')}")
PYEOF
)

if [ "${#ROWS[@]}" -eq 0 ]; then
  echo "No drafts are marked 'approved' yet."
  echo
  echo "Approve one first: open the dashboard's Drafted Runs panel (or edit"
  echo "run_drafts/index.json and set a draft's status to \"approved\"), then re-run this."
  echo
  echo "Current drafts:"
  "$PY" - "$INDEX" <<'PYEOF'
import json, sys
try:
    entries = json.load(open(sys.argv[1]))
except Exception:
    entries = []
if not entries:
    print("  (none)")
for e in sorted(entries, key=lambda x: x.get("id",0), reverse=True):
    print(f"  #{e.get('id')} [{e.get('status')}] {e.get('title','')}  ({e.get('file','')})")
PYEOF
  exit 0
fi

echo "Approved drafts:"
i=1
declare -a IDS TITLES FILES
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r id title file <<< "$row"
  IDS[$i]="$id"; TITLES[$i]="$title"; FILES[$i]="$file"
  printf "  %d) #%s  %s  (run_drafts/%s)\n" "$i" "$id" "$title" "$file"
  i=$((i+1))
done
echo

read -r -p "Pick a number to see its launch command (or Enter to cancel): " choice
if [ -z "${choice:-}" ] || ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -ge "$i" ]; then
  echo "Cancelled — nothing launched."
  exit 0
fi

SEL_FILE="${FILES[$choice]}"
SEL_TITLE="${TITLES[$choice]}"
SEL_ID="${IDS[$choice]}"
DRAFT_PATH="${PROJECT_DIR}/run_drafts/${SEL_FILE}"

if [ ! -f "$DRAFT_PATH" ]; then
  echo "Draft file missing: $DRAFT_PATH"
  exit 1
fi

echo
echo "----------------------------------------------"
echo "Selected: #${SEL_ID} — ${SEL_TITLE}"
echo "Draft:    ${DRAFT_PATH}"
echo "----------------------------------------------"
echo
read -r -p "Show the launch command and copy the path to your clipboard? [y/N] " ok
if [[ ! "${ok:-}" =~ ^[Yy]$ ]]; then
  echo "Cancelled — nothing printed or copied."
  exit 0
fi

# Copy the draft PATH to the clipboard (per spec). Never launches anything.
if command -v pbcopy >/dev/null 2>&1; then
  printf '%s' "$DRAFT_PATH" | pbcopy && COPIED=" (path copied to clipboard)"
else
  COPIED=""
fi

cat <<EOF

TO LAUNCH THIS RUN YOURSELF, run ONE of these in the project directory:

  # Open Claude Code and paste the prompt:
  cat "$DRAFT_PATH" | pbcopy   # then paste into a new Claude Code session

  # …or pass it directly (review it first!):
  claude "\$(cat "$DRAFT_PATH")"

The draft path is on your clipboard${COPIED:-}.

This script did NOT launch anything — that's your call. After you launch it, you can
mark it launched on the dashboard (or set its status to "launched" in
run_drafts/index.json) so Jarvis knows it's running.
EOF
