#!/usr/bin/env python3
"""
capability_watcher.py — cheap trigger for the CLARVIS capability queue.

WHY THIS EXISTS
    The queue used to be drained by a Claude Code scheduled task firing every 30
    minutes. That meant ~48 full agent sessions a day to answer a question that is
    one HTTP GET, for a queue that receives a request maybe once a week. The waste
    was never the polling — it was booting a reasoning agent to do the polling.

    So: this script does the polling (free), and spawns Claude Code only when there
    is actually something to build. Idle cost is a Supabase SELECT. Because that's
    nearly free, it can run every 2 minutes instead of every 30 — the fix now starts
    landing while Alex is still in the conversation where CLARVIS filed the request.

    The scheduled task remains, dropped to once a day, as a backstop: if it ever
    finds work this watcher should have caught, the watcher is dead (see HEARTBEAT).

HEARTBEAT
    Every run touches .capability_watcher_heartbeat with an ISO timestamp, whether
    or not there was work. A watcher that dies silently otherwise looks EXACTLY like
    a quiet queue — same class of bug as the mail-scan summaries this queue's first
    request was about. The daily backstop reads that file and reports a stale one.

NETWORK BLIPS vs OUTAGES
    Home-network DNS drops a Supabase read a few times a day (Errno 8). Each poll
    therefore retries the read a couple of times with backoff — a blip that clears
    in seconds never even makes the log. Failures that survive the retries bump
    .capability_watcher_failstreak (count + when it started); a poll that succeeds
    deletes it. The distinction matters because the heartbeat stays FRESH through a
    network outage — the process is running fine, it just can't see the queue — so
    the streak file is the only signal that separates "queue quiet" from "queue
    unreachable". The daily backstop checks it alongside the heartbeat.

Install (launchd, every 2 min):  scripts/install_capability_watcher.sh
Manual run:                      python3 scripts/capability_watcher.py [--dry-run]
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, "second-brain")
CHAT = os.path.join(ROOT, "second-brain-chat")
SKILL = os.path.join(HOME, ".claude", "scheduled-tasks",
                     "clarvis-capability-processor", "SKILL.md")
CLAUDE_BIN = os.path.join(HOME, ".local", "bin", "claude")

HEARTBEAT = os.path.join(ROOT, ".capability_watcher_heartbeat")
WATCHER_LOCK = os.path.join(ROOT, ".capability_watcher.lock")   # holds spawned PID
FAILSTREAK = os.path.join(ROOT, ".capability_watcher_failstreak")
LOG = os.path.join(ROOT, "scripts", "capability_watcher.log")

BUILD_TIMEOUT_S = 45 * 60      # a stuck build must not wedge the watcher forever
LOCK_STALE_S = 2 * 60 * 60     # matches the processor's own lock policy
READ_ATTEMPTS = 3              # queue reads per poll; home-DNS blips clear in seconds
READ_BACKOFF_S = 2             # sleep 2s then 4s between attempts
FAIL_STREAK_ALERT = 10         # 10 failed polls ≈ 20 min blind = outage, not blips

# The CLI exits 0 when it can't authenticate, so rc is useless for detecting it.
# Between Aug 9-14 2026 that turned five days of dead builds into five days of
# log lines reading "build finished rc=0" — nothing anywhere went red. If any of
# these show up in a build's output, the build did not happen.
AUTH_FAILURE_MARKERS = (
    "OAuth session expired",
    "Failed to authenticate",
    "Not logged in",
    "Please run /login",
    "Invalid API key",
)


def log(msg: str) -> None:
    """Only meaningful events land here — a no-op poll writes nothing but the
    heartbeat, so this log stays readable instead of 720 'queue empty' lines/day."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        with open(LOG, "a") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line.rstrip())


def beat() -> None:
    try:
        with open(HEARTBEAT, "w") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        log(f"heartbeat write failed: {e}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # exists, owned by someone else
    return True


def another_run_active() -> bool:
    """True when a spawned Claude Code build is still going. launchd already
    serializes same-label jobs, but a PID lock also covers a manual run racing a
    scheduled one — and self-heals if the process died without cleaning up."""
    if not os.path.exists(WATCHER_LOCK):
        return False
    age = datetime.now().timestamp() - os.path.getmtime(WATCHER_LOCK)
    try:
        with open(WATCHER_LOCK) as fh:
            pid = int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_alive(pid) and age < LOCK_STALE_S:
        return True
    log(f"clearing stale watcher lock (pid={pid or '?'}, age={int(age)}s)")
    try:
        os.remove(WATCHER_LOCK)
    except OSError:
        pass
    return False


def _read_with_retry(read):
    """Call read() up to READ_ATTEMPTS times with growing backoff.

    Home-network DNS drops a handful of reads a day ([Errno 8]); those clear in
    seconds, so one poll absorbing them beats a log line per blip. A config error
    (bad key, missing table) fails all attempts identically and still surfaces —
    the retries only cost ~6s once per poll, and only on the failure path."""
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return read()
        except Exception:
            if attempt == READ_ATTEMPTS:
                raise
            time.sleep(READ_BACKOFF_S * attempt)


def _read_fail_streak() -> int:
    try:
        with open(FAILSTREAK) as fh:
            return int(fh.readline().strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_fail_streak() -> int:
    """Consecutive polls whose queue read failed even after retries, persisted
    across runs (line 1 = count, line 2 = UTC start). The heartbeat stays fresh
    through a network outage — the process IS running — so without this file a
    week of DNS failure would look exactly like a quiet queue to the backstop."""
    since = None
    try:
        with open(FAILSTREAK) as fh:
            lines = fh.read().splitlines()
        streak = int((lines[0] if lines else "0").strip() or 0)
        since = lines[1].strip() if len(lines) > 1 else None
    except (OSError, ValueError):
        streak = 0
    streak += 1
    since = since or datetime.now(timezone.utc).isoformat()
    try:
        with open(FAILSTREAK, "w") as fh:
            fh.write(f"{streak}\n{since}\n")
    except OSError:
        pass
    return streak


def _clear_fail_streak() -> None:
    streak = _read_fail_streak()
    if not streak:
        return
    if streak >= FAIL_STREAK_ALERT:
        log(f"queue reads recovered after {streak} consecutive failed polls")
    try:
        os.remove(FAILSTREAK)
    except OSError:
        pass


def pending() -> list:
    """Open requests via the SAME code path the processor uses — no second
    implementation of 'what counts as open' to drift out of sync."""
    sys.path.insert(0, CHAT)
    from dotenv import load_dotenv
    from supabase import create_client
    import capability_escalation as esc

    for env_path in (os.path.join(ROOT, ".env"), os.path.join(CHAT, ".env")):
        load_dotenv(env_path)
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY not found in .env")
    esc.init(create_client(url, key))
    return _read_with_retry(esc.pending_requests)


def build_env() -> dict:
    """Environment for the spawned CLI, with credentials passed in explicitly.

    launchd hands this process a bare environment — PATH, SSH_AUTH_SOCK, and not
    much else. A token exported in ~/.zshrc or typed in a terminal never reaches
    it, which is exactly how an interactive `claude` that works by hand sits next
    to a watcher that can't authenticate at all. So read the token from .env (the
    repo's one secret store, gitignored) and hand it to the child directly.

    Absent token = no change: the CLI falls back to its own stored login.
    """
    env = os.environ.copy()
    try:
        from dotenv import dotenv_values
    except ImportError:
        return env
    for env_path in (os.path.join(ROOT, ".env"), os.path.join(CHAT, ".env")):
        try:
            token = (dotenv_values(env_path) or {}).get("CLAUDE_CODE_OAUTH_TOKEN")
        except OSError:
            continue
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            break
    return env


def check_auth_failure(proc) -> bool:
    """True when the build died on auth. Logs it loudly, because rc won't."""
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    hit = next((m for m in AUTH_FAILURE_MARKERS if m in blob), None)
    if not hit:
        return False
    log(f"!! AUTH FAILURE (rc={proc.returncode}, which is why this looked fine): "
        f"{hit!r}. The queue is NOT being drained. Fix: run `claude auth status` "
        f"— if it says loggedIn false, run `claude auth login`, or put a "
        f"CLAUDE_CODE_OAUTH_TOKEN in {os.path.join(ROOT, '.env')}.")
    return True


def spawn_build(count: int) -> None:
    """Hand off to Claude Code, pointed at the scheduled task's own SKILL.md so the
    guardrails (untrusted request text, hard refusals, never push red) have exactly
    ONE definition. Duplicating them into this prompt is how they'd drift apart."""
    prompt = (
        "This is an automated on-demand run of the clarvis-capability-processor "
        f"task, triggered by the capability watcher because {count} request(s) are "
        f"pending in the queue. Read {SKILL} and follow it exactly, including every "
        "guard rail and the hard refusals. Alex is not present — do not ask "
        "questions; make reasonable choices and note them in your output."
    )
    with open(WATCHER_LOCK, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        log(f"{count} pending request(s) → starting Claude Code build")
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--permission-mode", "auto"],
            cwd=ROOT, timeout=BUILD_TIMEOUT_S,
            capture_output=True, text=True, env=build_env(),
        )
        tail = (proc.stdout or "").strip().splitlines()
        log(f"build finished rc={proc.returncode}; last line: "
            f"{tail[-1][:300] if tail else '(no output)'}")
        check_auth_failure(proc)
        if proc.returncode != 0 and proc.stderr:
            log(f"stderr: {proc.stderr.strip()[:500]}")
    except subprocess.TimeoutExpired:
        log(f"build exceeded {BUILD_TIMEOUT_S}s and was killed — check the queue "
            f"for a request stuck in_progress")
    finally:
        try:
            os.remove(WATCHER_LOCK)
        except OSError:
            pass


def main() -> int:
    beat()
    dry = "--dry-run" in sys.argv
    if another_run_active():
        return 0
    try:
        reqs = pending()
    except Exception as e:
        streak = _bump_fail_streak()
        # Loud line at the threshold, then a reminder every ~30 min — an outage
        # deserves a siren, not 30 identical lines an hour.
        if streak == FAIL_STREAK_ALERT or (streak > FAIL_STREAK_ALERT
                                           and streak % 15 == 0):
            log(f"!! {streak} consecutive failed queue checks (~{streak * 2} min "
                f"blind, each retried {READ_ATTEMPTS}x) — the queue is UNREACHABLE, "
                f"not quiet. The heartbeat stays fresh through this, so nothing "
                f"else will flag it. Latest: {str(e)[:200]}")
        else:
            log(f"queue check failed after {READ_ATTEMPTS} attempts "
                f"(streak {streak}): {str(e)[:300]}")
        return 1
    _clear_fail_streak()
    # Fire only on genuinely NEW requests. Something left in_progress means a build
    # died mid-flight; re-spawning every 2 minutes would just restart it forever, so
    # the daily backstop owns that case instead.
    fresh = [r for r in reqs if r.get("status") == "pending"]
    if not fresh:
        if reqs:
            log(f"{len(reqs)} request(s) in_progress, none new — leaving them alone")
        return 0
    if dry:
        log(f"[dry-run] would build: {', '.join(r.get('slug', '?') for r in fresh)}")
        return 0
    spawn_build(len(fresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
