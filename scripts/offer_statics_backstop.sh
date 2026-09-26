#!/bin/zsh
# Mac backstop for the statics swap. iCloud can evict the QA folder (dataless files hang or read
# empty), so read the folder from the vault's git mirror instead of the iCloud path.
set -u
VAULT_GIT="$HOME/.second-brain-vault.git"
TMP="$(mktemp -d /tmp/offer-statics-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
# Every follow-up folder, qa-<date> and followup-qa-<date> (the touch-2 second statics from
# 2026-09-28); offer_statics picks the ones dated today or earlier and merges their approvals.
# Taking only the newest qa-* folder would never see a followup-qa-* one.
folders=("${(@f)$(git --git-dir="$VAULT_GIT" ls-tree --name-only HEAD "Money/Clients/spec-ads/" | grep -E '/(followup-)?qa-[0-9-]+$')}")
if [ -z "${folders[1]:-}" ]; then echo "$(date '+%F %T') no qa-* or followup-qa-* folder in the vault mirror"; exit 0; fi
git --git-dir="$VAULT_GIT" archive HEAD "${folders[@]}" | tar -x -C "$TMP" --strip-components=3
echo "$(date '+%F %T') using ${#folders[@]} follow-up folder(s) from vault git $(git --git-dir="$VAULT_GIT" rev-parse --short HEAD)"
cd "$HOME/second-brain" && exec /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scripts/offer_statics.py swap --apply --spec-dir "$TMP"
