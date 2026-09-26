#!/bin/zsh
# After buying Hunter Starter: verify every waiting founder address, stalest ads first.
#   scripts/hunter_starter_pass.sh --dry-run    (see the order and the count first)
cd "$HOME/second-brain" || exit 1
set -a; source ./.env; set +a
unset HUNTER_USE_RESERVE
exec python3 scripts/hunter_starter_pass.py "$@"
