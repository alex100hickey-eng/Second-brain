#!/bin/bash
# Auto-commits and pushes the Obsidian vault to second-brain-vault on GitHub.
# Run periodically by ~/Library/LaunchAgents/com.secondbrain.vaultsync.plist

VAULT="/Users/alexhickey24/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain"

cd "$VAULT" || exit 1

git add -A

if ! git diff --cached --quiet; then
  git commit -m "Auto-sync $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  git push origin main
fi
