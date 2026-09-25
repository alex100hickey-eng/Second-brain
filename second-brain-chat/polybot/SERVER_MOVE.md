# polybot → the Hetzner server

Why: on the Mac the loop loses every hour the lid is closed, the campus network drops DNS (9/23: blind
11:30–11:55 and 12:02–13:19), and the US venue's rate limit is shared with the whole CWRU IP. On the server
it runs all night, on its own IP and quota. Nothing about what it trades changes: same code, same config,
same ledger, same `gate_since_ts`.

## The 60-second checklist

**Once, before the day** (Alex, in Coolify → the `second-brain` app):
1. **Storages → + Add → Volume.** Name `polybot-data`, destination path **`/data/polybot`**.
2. **Environment variables:**
   - `POLYBOT_DATA_DIR=/data/polybot`
   - `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` (the same values as `~/second-brain/.env`)
   - `POLYBOT_ON_SERVER=0` (still off)
3. **Redeploy** once, so the volume is mounted. Nothing starts: `POLYBOT_ON_SERVER=0`.

**The move** (≈2 minutes, in this order; never both loops at once):

| # | what | done when |
|---|---|---|
| 1 | Stop the Mac loop: `launchctl bootout gui/$(id -u)/com.secondbrain.polybot && launchctl disable gui/$(id -u)/com.secondbrain.polybot` | `pgrep -f "polybot.runner loop"` prints nothing |
| 2 | Snapshot: `cd ~/second-brain/second-brain-chat && python3 -m polybot.migrate snapshot --out ~/polybot-move` | prints the gate fingerprint (per-module signals/decisions/closed/P&L) |
| 3 | Copy: `scp -r ~/polybot-move root@178.156.209.40:/tmp/` then on the server `docker cp /tmp/polybot-move/. <container>:/data/polybot/` | files are in the volume |
| 4 | Verify on the server: `docker exec <container> sh -c 'cd /app/second-brain-chat && python3 -m polybot.migrate verify --dir /data/polybot'` | **`verify: OK`**. Anything else: stop, go to rollback |
| 5 | Coolify: `POLYBOT_ON_SERVER=1` → **Restart** | — |
| 6 | Watch: `docker exec <container> tail -f /data/polybot/loop.log` | `polybot loop started on server` within ~10 min (it waits out the Mac's lease) |

`<container>` is the name from `docker ps | grep second-brain`. `/app` is where nixpacks puts the code; check
with `docker exec <container> ls /app/second-brain-chat/polybot`.

**Kill switch on the server:** `docker exec <container> touch /data/polybot/KILL` (remove the file to resume).

**Rollback** (the reverse, same tools):
1. Coolify: `POLYBOT_ON_SERVER=0` → Restart.
2. On the server: `python3 -m polybot.migrate snapshot --out /tmp/polybot-back`, then scp it to the Mac.
3. On the Mac, copy the files into `~/second-brain/second-brain-chat/polybot/`, then `python3 -m polybot.migrate verify --dir <that folder>` must print OK.
4. Restart the Mac loop: `launchctl enable gui/$(id -u)/com.secondbrain.polybot && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.secondbrain.polybot.plist`.

---

## How it runs there
- **Where:** inside the existing Coolify app (nixpacks build, `gunicorn app:app`). No new service and no compose file.
- **Why a child process:** `app.py` starts `polybot_supervisor.start()` on the server node. When all of its switches are set, the supervisor runs `python -u -m polybot.runner loop` as a **child process**, with the working directory `second-brain-chat/` and the log at `/data/polybot/loop.log` (rotated at 50 MB). It is not a thread like the other server loops, because polybot's watchdog `os._exit`s its process on a stall, and a thread doing that would take the web app down.
- **Restarts:** the supervisor restarts the child when it exits, as launchd's KeepAlive does on the Mac. A crash loop backs off 30 s → 15 min.
- **One loop per container:** there's an `flock` on `/data/polybot/.supervisor.lock`, so multiple gunicorn workers still start only one loop.
- **Default is off.** `polybot_supervisor.enabled()` refuses unless all of these hold:
  - `POLYBOT_ON_SERVER=1`
  - `POLYBOT_DATA_DIR` is a directory
  - `polybot.db` is already in it
  - the four keys are set (both Polymarket keys, both Supabase keys)
  Until then it prints `polybot on server: OFF (…why…)` at boot.

## Secrets and files
- **Env:** `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET_KEY` (Polymarket US), `SUPABASE_URL` and `SUPABASE_KEY` (already set for the app), `POLYBOT_DATA_DIR`, `POLYBOT_ON_SERVER`. `POLYBOT_NODE` is set to `server` by the supervisor.
- **In the volume:** the ledger `polybot.db`, `config.json` (modes, caps, `gate_since_ts`), `pairs.json`, `calibration.json`, `calibration-samples.json`, `jobs-state.json`, `compounding-state.json`, `loop.log`, `report-latest.txt` and `KILL`. The code resolves all of them through `config.DATA_DIR`.
- On the Mac `POLYBOT_DATA_DIR` is unset, so everything stays next to the code, exactly as before.

## Carrying the ledger without losing evidence
`polybot.migrate snapshot` makes a **consistent** copy (sqlite's online backup API; never `cp` a live sqlite file) plus `manifest.json` with:
- a sha256 per file
- sqlite `integrity_check`
- the **gate fingerprint**: per module, the signals, decisions, closed trades and closed P&L since `gate_since_ts`, plus `gate_since_ts` itself

`verify` recomputes all of that where the files landed and exits non-zero on any difference, **including a changed `gate_since_ts`**. A move is not a rule change and must not reset the evidence.

Tested 9/24 against the live ledger: 81 MB plus 5 files in 1.4 s, integrity `ok`, and the fingerprint verified.

## Never two loops
1. **Order:** step 1 stops the Mac loop before anything starts on the server.
2. **Lease:** `polybot/lease.py` keeps a `polybot:lease` row in the shared Supabase state store. Every loop renews it each minute. A loop won't start while another node's lease is under 10 minutes old; it logs `NOT started … holds the polybot lease` and retries. The Mac side fails open on a store error, which it has always survived. The server supervisor fails **closed**, so the new node is the one that waits.

## Things that change after the move (for whoever owns them)
- **`scripts/money_progress.py`** (the /money scorecard) runs `python -m polybot.runner report` against the **Mac** ledger and checks the **Mac** `loop.log` age. After the move it must read the server instead. The loop already publishes `business:polybot` to Supabase every tick, and the `heartbeat:polybot` row shows it's alive.
- **The Mac's `polybot/` data** becomes a stale copy. Leave it; it's the rollback's starting point only via a fresh snapshot, never directly.
- **Memory and disk:** the loop is small next to the 2 GB box. The ledger is 81 MB and snapshots are pruned at 30 days.
