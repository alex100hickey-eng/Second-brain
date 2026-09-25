"""Run the polybot loop on the server, as a supervised child process of the web app. OFF by default.

Why a child process and not a thread like the other server loops: polybot's watchdog calls
os._exit(1) when the loop stalls (launchd restarts it on the Mac), and a thread doing that would take
the whole gunicorn worker — the web app — down with it. So the app starts ONE child
(`python -u -m polybot.runner loop`), restarts it when it exits (the Mac's launchd KeepAlive), and
backs off if it keeps dying.

Switches (Coolify env), all required before anything runs:
    POLYBOT_ON_SERVER=1            start it at all (unset/0 = never, which is the default)
    POLYBOT_DATA_DIR=/data/polybot a PERSISTENT volume: ledger, config.json, pairs, calibration, KILL
    POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY, SUPABASE_URL / SUPABASE_KEY (already there)
One child per container even with several gunicorn workers (an flock on DATA_DIR/.supervisor.lock),
and none at all while another node holds the polybot lease (polybot/lease.py) — the Mac loop must be
stopped first. Runbook: polybot/SERVER_MOVE.md.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_MAX_BYTES = 50 * 1024 * 1024


def enabled(env=os.environ) -> tuple[bool, str]:
    if env.get("POLYBOT_ON_SERVER", "0") != "1":
        return False, "POLYBOT_ON_SERVER is not 1"
    data = env.get("POLYBOT_DATA_DIR")
    if not data or not os.path.isdir(data):
        return False, f"POLYBOT_DATA_DIR {data!r} is not a directory (mount the persistent volume first)"
    if not os.path.exists(os.path.join(data, "polybot.db")):
        return False, f"no ledger at {data}/polybot.db — carry it over first (polybot.migrate)"
    for k in ("POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY", "SUPABASE_URL", "SUPABASE_KEY"):
        if not env.get(k):
            return False, f"{k} not set"
    return True, "ok"


def _rotate(log_path: str) -> None:
    try:
        if os.path.getsize(log_path) > LOG_MAX_BYTES:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass


def _other_node_live() -> str | None:
    """The server waits for a store it can read: unlike the Mac loop, it fails CLOSED."""
    sys.path.insert(0, APP_DIR)
    from polybot import lease
    try:
        return lease.conflict(os.environ.get("POLYBOT_NODE", "server"), lease.holder())
    except Exception as exc:
        return f"lease store unreachable ({type(exc).__name__})"


def supervise(stop: threading.Event | None = None, spawn=None, sleep=time.sleep, log=print) -> None:
    data = os.environ["POLYBOT_DATA_DIR"]
    lock = open(os.path.join(data, ".supervisor.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("polybot supervisor: another worker in this container already runs the loop")
        return
    env = dict(os.environ, POLYBOT_NODE=os.environ.get("POLYBOT_NODE", "server"))
    backoff = 30.0
    while not (stop and stop.is_set()):
        why = _other_node_live()
        if why:
            log(f"polybot supervisor: not starting — {why}")
            sleep(120)
            continue
        log_path = os.path.join(data, "loop.log")
        _rotate(log_path)
        started = time.time()
        with open(log_path, "a") as out:
            proc = (spawn or subprocess.Popen)([sys.executable, "-u", "-m", "polybot.runner", "loop"],
                                               cwd=APP_DIR, env=env, stdout=out, stderr=subprocess.STDOUT)
            code = proc.wait()
        ran = time.time() - started
        backoff = 30.0 if ran > 600 else min(backoff * 2, 900.0)   # a crash loop backs off to 15 min
        log(f"polybot supervisor: loop exited {code} after {ran:.0f}s — restarting in {backoff:.0f}s")
        sleep(backoff)


def start(log=print) -> bool:
    ok, why = enabled()
    if not ok:
        log(f"polybot on server: OFF ({why})")
        return False
    threading.Thread(target=supervise, kwargs={"log": log}, daemon=True, name="polybot-supervisor").start()
    log("polybot on server: supervisor started")
    return True
