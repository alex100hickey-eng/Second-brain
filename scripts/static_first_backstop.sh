#!/bin/zsh
# Mac backstop for static-first: at 07:30, before the server's 07:50 release, swap each approved
# brand's queued first touch for the version with its static attached. A no-op while
# STATIC_FIRST is off in scripts/offer_statics.py, and it only touches brands Alex marked
# `approve`. iCloud can evict the QA folder (dataless files hang or read empty), so the folder
# comes from the vault's git mirror, the same way offer_statics_backstop.sh reads its own.
set -u
VAULT_GIT="$HOME/.second-brain-vault.git"
TMP="$(mktemp -d /tmp/static-first-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
# The newest folder dated TODAY OR EARLIER. Folders are named for the day their first touches go
# out and are built days ahead: plain "newest" picked Wednesday's folder on Monday and would have
# attached none of Monday's statics. STATIC_FIRST_TODAY is a test hook.
today="${STATIC_FIRST_TODAY:-$(date +%F)}"
latest="$(git --git-dir="$VAULT_GIT" ls-tree --name-only HEAD "Money/Clients/spec-ads/" | grep -E '/first-touch-qa-[0-9-]+$' \
  | awk -F/ -v t="first-touch-qa-$today" 'substr($NF, 1, length(t)) <= t' | sort | tail -1)"
if [ -z "$latest" ]; then echo "$(date '+%F %T') no first-touch-qa-* folder dated $today or earlier in the vault mirror"; exit 0; fi
git --git-dir="$VAULT_GIT" archive HEAD "$latest" | tar -x -C "$TMP" --strip-components=4
echo "$(date '+%F %T') using $latest from vault git $(git --git-dir="$VAULT_GIT" rev-parse --short HEAD)"
cd "$HOME/second-brain" && exec /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scripts/offer_statics.py swap-first --apply --dir "$TMP"
