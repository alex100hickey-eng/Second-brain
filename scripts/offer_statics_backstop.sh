#!/bin/zsh
# Mac backstop for the statics swap. iCloud can evict the QA folder (dataless files hang or read
# empty), so read the folder from the vault's git mirror instead of the iCloud path.
set -u
VAULT_GIT="$HOME/.second-brain-vault.git"
TMP="$(mktemp -d /tmp/offer-statics-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
latest="$(git --git-dir="$VAULT_GIT" ls-tree --name-only HEAD "Money/Clients/spec-ads/" | grep -E '/qa-[0-9-]+$' | sort | tail -1)"
if [ -z "$latest" ]; then echo "$(date '+%F %T') no qa-* folder in the vault mirror"; exit 0; fi
git --git-dir="$VAULT_GIT" archive HEAD "$latest" | tar -x -C "$TMP" --strip-components=4
echo "$(date '+%F %T') using $latest from vault git $(git --git-dir="$VAULT_GIT" rev-parse --short HEAD)"
cd "$HOME/second-brain" && exec /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scripts/offer_statics.py swap --apply --dir "$TMP"
