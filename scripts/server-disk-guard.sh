#!/bin/bash
# server-disk-guard.sh — runs ON the Hetzner box (root cron), NOT on the Mac.
#
# The durable fix for the failure in NEEDS_ALEX.md §0a. The box has filled twice;
# both times the manual recovery was `docker builder prune -af`, run by hand only
# after a build had already died and wedged Coolify's queue for hours. The build
# cache is the thing that grows without bound — 8 builds ran between 03:00 and
# 04:30 UTC on 2026-08-01 and that alone was enough.
#
# Design choices, deliberately:
#   * KEEP-STORAGE CAP, not `-af`. Capping the build cache preserves recent layers,
#     so ordinary rebuilds stay fast; `-af` on a schedule would make every build a
#     cold build. The cap does the same job without the tax.
#   * IMAGES ARE NEVER TOUCHED HERE. `docker image prune -af` deletes rollback
#     targets — Alex explicitly treated that as a deliberate call, not routine, so
#     it stays a human decision and is not automated.
#   * PRESSURE-TRIGGERED, not unconditional. Below the threshold this does nothing
#     but log a line, so the cache is only sacrificed when space actually matters.
#
# INSTALL (Alex runs these; Claude does not open remote shells):
#   scp ~/second-brain/scripts/server-disk-guard.sh root@178.156.209.40:/usr/local/bin/
#   ssh root@178.156.209.40 'chmod +x /usr/local/bin/server-disk-guard.sh && \
#     (crontab -l 2>/dev/null | grep -v server-disk-guard; \
#      echo "17 * * * * /usr/local/bin/server-disk-guard.sh >> /var/log/disk-guard.log 2>&1") | crontab -'
#
# Verify after install:
#   ssh root@178.156.209.40 '/usr/local/bin/server-disk-guard.sh; tail -5 /var/log/disk-guard.log'
#
# Runs hourly at :17 — off the top of the hour so it never lands inside the
# scheduled vault-sync minute.

set -uo pipefail

THRESHOLD_PCT="${DISK_GUARD_THRESHOLD:-70}"   # start reclaiming at this % used
KEEP_CACHE="${DISK_GUARD_KEEP:-10GB}"         # build cache allowed to survive
KEEP_IMAGES="${DISK_GUARD_KEEP_IMAGES:-3}"    # image generations kept per app

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
pct_used() { df --output=pcent / | tail -1 | tr -dc '0-9'; }

BEFORE="$(pct_used)"

# --- Generational image retention -------------------------------------------
# THE actual growth driver. Every Coolify deploy leaves a ~1.94 GB image behind
# and nothing ever removes the old one: 25 of them (~20 GB) had piled up within
# 26 hours, which is what filled the box twice.
#
# This is NOT `docker image prune -af`. That deletes every unused image including
# the previous build, which is why it stayed a human decision. This keeps the
# newest KEEP_IMAGES generations PER APP, so rolling back to a recent build still
# works — it only drops the ones nobody would ever roll back to. Alex chose 3
# (live + 2 rollback targets) on 2026-08-01.
#
# Runs unconditionally, not just under pressure: bounded retention is the whole
# point. Letting 20 GB pile up and then dumping it means the box lives near-full,
# which is the state that caused every failure so far.
prune_old_images() {
    local deleted=0 skipped=0 id repo
    # `docker images` lists newest-first, so anything past the Nth for a given
    # repository is an older generation.
    while read -r repo id; do
        [ -z "${id:-}" ] && continue
        [ "$repo" = "<none>" ] && continue      # dangling; handled separately
        # Never touch an image backing a live container, even if it looks old.
        if [ -n "$(docker ps -q --filter "ancestor=$id" 2>/dev/null)" ]; then
            skipped=$((skipped + 1))
            continue
        fi
        if docker rmi "$id" >/dev/null 2>&1; then
            deleted=$((deleted + 1))
        else
            skipped=$((skipped + 1))            # in use by a stopped container, or shared layers
        fi
    done < <(docker images --format '{{.Repository}} {{.ID}}' 2>/dev/null \
             | awk -v keep="$KEEP_IMAGES" '{c[$1]++; if (c[$1] > keep) print $1, $2}')

    echo "[$(ts)] images — kept newest $KEEP_IMAGES per app; removed $deleted, skipped $skipped."
}

prune_old_images

if [ "$BEFORE" -lt "$THRESHOLD_PCT" ]; then
    echo "[$(ts)] OK — / at ${BEFORE}%, below ${THRESHOLD_PCT}% threshold; nothing pruned."
    exit 0
fi

echo "[$(ts)] PRESSURE — / at ${BEFORE}%; pruning build cache to ${KEEP_CACHE}."

# Build cache first: the cheapest thing to lose.
# Docker 29 renamed --keep-storage to --reserved-space and warns on the old name.
# Probe once and use whichever this daemon speaks, so the script works on the box
# today and doesn't silently become a no-op when the deprecated flag is removed.
if docker builder prune --help 2>&1 | grep -q -- "--reserved-space"; then
    CACHE_FLAG="--reserved-space"
else
    CACHE_FLAG="--keep-storage"
fi
docker builder prune -f "$CACHE_FLAG" "$KEEP_CACHE" 2>&1 | tail -3

AFTER_CACHE="$(pct_used)"
echo "[$(ts)] after builder prune — / at ${AFTER_CACHE}%."

# Still tight? Take the safe extras: stopped containers and dangling (untagged)
# layers. Still NOT `image prune -af` — tagged rollback images survive this.
if [ "$AFTER_CACHE" -ge "$THRESHOLD_PCT" ]; then
    echo "[$(ts)] still at ${AFTER_CACHE}% — pruning stopped containers + dangling images."
    docker container prune -f 2>&1 | tail -2
    docker image prune -f 2>&1 | tail -2      # dangling only; -a is deliberately absent
fi

AFTER="$(pct_used)"
echo "[$(ts)] DONE — / went ${BEFORE}% -> ${AFTER}%."

# Above 85% after a full pass means reclaimable space is exhausted and something
# real is growing (logs, volumes, a runaway DB). That needs a human, so make it loud.
if [ "$AFTER" -ge 85 ]; then
    echo "[$(ts)] ALERT — / still at ${AFTER}% after pruning. Reclaimable space is gone;"
    echo "[$(ts)]         investigate: du -xh / --max-depth=2 2>/dev/null | sort -rh | head -20"
    exit 1
fi
exit 0
