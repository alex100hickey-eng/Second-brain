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
   - `POLYBOT_ON_SERVER=1`. Safe to set now: with no verified ledger in the volume, the server only waits and logs `polybot on server: armed, waiting — no ledger ...`.
3. **Redeploy** once, so the volume is mounted and the env is live.

**The move: one command** (from the Mac, after Alex says "move polybot to the server"):

| | command | what it does |
|---|---|---|
| read | `cd ~/second-brain/second-brain-chat && python3 -m polybot.server_move plan` | prints every command `go` will run, local and remote. Alex names it; nothing runs |
| rehearse | `python3 -m polybot.server_move dry-run` | every local step on a copy: snapshot, the volume copy, `verify --mark`, and the supervisor's own readiness check. The Mac loop keeps running and nothing leaves the Mac (tested 9/25: 82 MB, 2.4 s, READY) |
| **move** | `python3 -m polybot.server_move go --yes` | preflight on the host (the container, the volume, the new code, and the env check, which prints `env-ok` and never a value; the volume must hold no ledger yet) → stop and disable the Mac loop, release its lease → snapshot → scp + docker cp → `migrate verify --dir /data/polybot --mark` in the container → waits for `polybot loop started on server`. **Any failure after the Mac loop stops undoes itself:** it unmarks the server and restarts the Mac loop |
| back | `python3 -m polybot.server_move rollback --yes` | unmark and stop the server loop (the Mac never starts while the server loop runs) → server snapshot copied back → the Mac's stale files kept as `*.pre-rollback-*` → verify → start the Mac loop |

**Why no Coolify click at move time:** the server loop needs **both** `POLYBOT_ON_SERVER=1` **and** a `MOVE_VERIFIED` marker in `/data/polybot`. Only `polybot.migrate verify --mark` writes the marker, and only when every file and the gate fingerprint match. The supervisor checks every 60 s, and re-checks before every restart of the loop, so removing the marker is also a clean server-side stop.

**Kill switch on the server:** `docker exec <container> touch /data/polybot/KILL` (remove the file to resume).

**By hand, if the script can't be used** (the same steps):
1. `launchctl bootout gui/$(id -u)/com.secondbrain.polybot && launchctl disable gui/$(id -u)/com.secondbrain.polybot`, then `python3 -m polybot.lease release`
2. `python3 -m polybot.migrate snapshot --out ~/polybot-move`
3. `scp -r ~/polybot-move root@178.156.209.40:/tmp/`, then on the host `docker cp /tmp/polybot-move/. <container>:/data/polybot/`
4. `docker exec <container> sh -c 'cd /app/second-brain-chat && python3 -m polybot.migrate verify --dir /data/polybot --mark'`. It must print `verify: OK` and `marked:`
5. `docker exec <container> tail -f /data/polybot/loop.log` until `polybot loop started on server` (≤ ~2 min)

`<container>` is the name from `docker ps | grep h72tei3gy97z4wlqyqpvuylg`.

---

## How it runs there
- **Where:** inside the existing Coolify app (nixpacks build, `gunicorn app:app`). No new service and no compose file.
- **Why a child process:** `app.py` starts `polybot_supervisor.start()` on the server node. When all of its switches are set, the supervisor runs `python -u -m polybot.runner loop` as a **child process**, with the working directory `second-brain-chat/` and the log at `/data/polybot/loop.log` (rotated at 50 MB). It is not a thread like the other server loops, because polybot's watchdog `os._exit`s its process on a stall, and a thread doing that would take the web app down.
- **Restarts:** the supervisor restarts the child when it exits, as launchd's KeepAlive does on the Mac. A crash loop backs off 30 s → 15 min.
- **One loop per container:** there's an `flock` on `/data/polybot/.supervisor.lock`, so multiple gunicorn workers still start only one loop.
- **Default is off.** `polybot_supervisor.enabled()` refuses unless all of these hold:
  - `POLYBOT_ON_SERVER=1`
  - `POLYBOT_DATA_DIR` is a directory
  - `polybot.db` is already in it, **and** `MOVE_VERIFIED` (written only by a clean `migrate verify --mark`)
  - the four keys are set (both Polymarket keys, both Supabase keys)
  With `POLYBOT_ON_SERVER` unset or 0 it prints `polybot on server: OFF` at boot. With it set to 1 but not ready, it waits (`armed, waiting — …why…`) and starts on its own once the verified ledger is there.

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
- **`scripts/money_progress.py`** (the /money scorecard) follows the loop by itself: while the Mac's `loop.log` is fresh it reads the Mac ledger as before; once that log is quiet it reads `business:polybot` (the 1-day report, published every 15 min with the node's name) and `heartbeat:polybot` from Supabase, and uses them only when the node is `server`. After the move its polybot line says "alive on the server".
- **The Mac's `polybot/` data** becomes a stale copy. Leave it; it's the rollback's starting point only via a fresh snapshot, never directly.
- **Memory and disk:** the loop is small next to the 2 GB box. The ledger is 81 MB and snapshots are pruned at 30 days.
