"""The server move as one command. polybot/SERVER_MOVE.md is the same steps by hand.

    python3 -m polybot.server_move plan            # every command `go` runs, local and remote; runs nothing
    python3 -m polybot.server_move dry-run         # every LOCAL step on a copy: snapshot, the "volume" copy,
                                                   #   verify --mark, the supervisor's own readiness check.
                                                   #   The Mac loop keeps running; nothing is sent anywhere.
    python3 -m polybot.server_move go --yes        # the move (only once Alex has said "move polybot to the server")
    python3 -m polybot.server_move rollback --yes  # back to the Mac

Run it on the Mac from ~/second-brain/second-brain-chat, the tree the launchd loop runs from.

What it needs first, all Alex's in Coolify, once: the volume mounted at /data/polybot, and the env
POLYBOT_DATA_DIR=/data/polybot, POLYBOT_ON_SERVER=1, POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY, then one
redeploy. With no verified ledger in the volume that deploy starts nothing (the supervisor waits for
the MOVE_VERIFIED marker), so there is no Coolify click at move time.

`go`, in order, stopping at the first failure:
  preflight  find the container; the volume, the new code and the env are there (the env check
             prints "env-ok" or nothing: never a value); the volume holds no ledger yet
  1  stop and disable the Mac loop; wait until no loop process is left; release the Mac's lease
  2  snapshot the ledger (sqlite online backup + sha256 + the gate fingerprint)
  3  scp it to the host and docker cp it into the volume
  4  verify it inside the container and write MOVE_VERIFIED (only on a clean verify)
  5  wait for "polybot loop started on server" in the server's loop.log (the supervisor checks
     every 60 s)
A failure after step 1 unmarks the server and re-enables the Mac loop, so a half-done move leaves
the loop where it was. Nothing here places an order, changes a rule or touches gate_since_ts.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from . import config, lease, migrate

HOST = "root@178.156.209.40"
APP_UUID = "h72tei3gy97z4wlqyqpvuylg"          # the Coolify app; its containers are named <uuid>-<n>
REMOTE_DATA = "/data/polybot"
REMOTE_APP = "/app/second-brain-chat"           # where nixpacks puts the repo (preflight checks it)
LABEL = "com.secondbrain.polybot"
LOOP_PATTERN = "polybot.runner loop"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
START_WAIT_S = 5 * 60
STOP_WAIT_S = 90


def _run(cmd: list, timeout: float = 180) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)


class Move:
    def __init__(self, host: str = HOST, run=_run, sleep=time.sleep, log=print, uid: int | None = None,
                 data_dir: str | None = None, home: str | None = None, clock=time.time):
        self.host, self.run, self.sleep, self.log, self.clock = host, run, sleep, log, clock
        self.uid = os.getuid() if uid is None else uid
        self.data = data_dir or config.DATA_DIR
        self.home = home or os.path.expanduser("~")
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.container = None

    # ---- the command strings (one source for `plan` and for the run) --------------------------
    def ssh(self, remote: str) -> list:
        return ["ssh", *SSH_OPTS, self.host, remote]

    def dexec(self, sh: str, container: str | None = None) -> str:
        return f"docker exec {container or self.container or '<container>'} sh -c {shlex.quote(sh)}"

    def find_container_cmd(self) -> list:
        return self.ssh(f"docker ps --format '{{{{.Names}}}}' | grep -m1 {APP_UUID}")

    def preflight_cmds(self) -> list:
        code = (f"test -d {REMOTE_DATA} && test -f {REMOTE_APP}/polybot_supervisor.py && "
                f"grep -q MOVE_VERIFIED {REMOTE_APP}/polybot_supervisor.py && command -v python3 >/dev/null && echo code-ok")
        env = ('test "$POLYBOT_ON_SERVER" = 1 && test "$POLYBOT_DATA_DIR" = ' + REMOTE_DATA +
               ' && test -n "$POLYMARKET_KEY_ID" && test -n "$POLYMARKET_SECRET_KEY" && test -n "$SUPABASE_URL" '
               '&& echo env-ok')
        empty = f"test ! -e {REMOTE_DATA}/polybot.db && echo volume-empty"
        return [("code-ok", self.ssh(self.dexec(code))), ("env-ok", self.ssh(self.dexec(env))),
                ("volume-empty", self.ssh(self.dexec(empty)))]

    def mac_stop_cmds(self) -> list:
        return [["launchctl", "bootout", f"gui/{self.uid}/{LABEL}"], ["launchctl", "disable", f"gui/{self.uid}/{LABEL}"]]

    def mac_start_cmds(self) -> list:
        plist = os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")
        return [["launchctl", "enable", f"gui/{self.uid}/{LABEL}"], ["launchctl", "bootstrap", f"gui/{self.uid}", plist]]

    def snap_dir(self, kind: str = "move") -> str:
        return os.path.join(self.home, f"polybot-{kind}-{self.stamp}")

    def copy_cmds(self) -> list:
        snap = self.snap_dir()
        remote_tmp = f"/tmp/{os.path.basename(snap)}"
        return [["scp", "-r", *SSH_OPTS, snap, f"{self.host}:/tmp/"],
                self.ssh(f"docker cp {remote_tmp}/. {self.container or '<container>'}:{REMOTE_DATA}/")]

    def verify_cmd(self) -> list:
        return self.ssh(self.dexec(f"cd {REMOTE_APP} && python3 -m polybot.migrate verify --dir {REMOTE_DATA} --mark"))

    def started_cmd(self) -> list:
        return self.ssh(self.dexec(f'grep -c "polybot loop started on server" {REMOTE_DATA}/loop.log || true'))

    def server_stop_cmd(self) -> list:
        return self.ssh(self.dexec(f'cd {REMOTE_APP} && python3 -m polybot.migrate unmark; '
                                   f'pkill -f "{LOOP_PATTERN}"; true'))

    def plan(self, kind: str = "go") -> str:
        # A [server] line is the command run ON the host, each through
        # `ssh -o BatchMode=yes -o ConnectTimeout=15 <host> '<line>'`; a [mac] line runs here as shown.
        fmt = lambda c: c[-1] if c[0] == "ssh" else " ".join(shlex.quote(x) for x in c)
        lines = [f"server_move {kind}: host {self.host}; <container> = the first running container named "
                 f"{APP_UUID}-*; nothing runs until `{kind} --yes`.",
                 f"  [server] lines run on the host via: ssh {' '.join(SSH_OPTS)} {self.host} '<line>'"]
        if kind == "go":
            lines += ["  preflight (read-only):", f"    [server] {fmt(self.find_container_cmd())}"]
            lines += [f"    [server] {fmt(c)}   -> expects {want}" for want, c in self.preflight_cmds()]
            lines += ["  1 stop the Mac loop:"] + [f"    [mac] {fmt(c)}" for c in self.mac_stop_cmds()]
            lines += [f"    [mac] wait until `pgrep -f '{LOOP_PATTERN}'` finds nothing ({STOP_WAIT_S}s max)",
                      "    [mac] python3 -m polybot.lease release   (this Mac's lease, so the server need not wait 10 min)",
                      f"  2 [mac] python3 -m polybot.migrate snapshot --out {self.snap_dir()}",
                      "  3 copy:"] + [f"    [{'mac' if c[0] == 'scp' else 'server'}] {fmt(c)}" for c in self.copy_cmds()]
            lines += [f"  4 [server] {fmt(self.verify_cmd())}   -> expects 'verify: OK' and 'marked:'",
                      f"  5 [server] {fmt(self.started_cmd())}   every 20s until > 0 ({START_WAIT_S // 60} min max)",
                      "  on a failure after step 1:",
                      f"    [server] {fmt(self.server_stop_cmd())}"] + [f"    [mac] {fmt(c)}" for c in self.mac_start_cmds()]
        else:
            lines += ["  rollback:", f"    [server] {fmt(self.find_container_cmd())}",
                      f"  1 [server] {fmt(self.server_stop_cmd())}",
                      f"    [server] {fmt(self.ssh(self.dexec(f'pgrep -f \"{LOOP_PATTERN}\" || echo stopped')))}"
                      f"   until 'stopped' ({STOP_WAIT_S}s max)",
                      f"    [server] {fmt(self.ssh(self.dexec(f'cd {REMOTE_APP} && python3 -m polybot.lease release --node server')))}",
                      f"  2 [server] snapshot inside the container, docker cp it out, scp it to {self.snap_dir('back')}",
                      f"  3 [mac] move the Mac's stale files aside (*.pre-rollback-{self.stamp}), copy the snapshot in, "
                      "verify (must be clean)",
                      "  4 start the Mac loop:"] + [f"    [mac] {fmt(c)}" for c in self.mac_start_cmds()]
        return "\n".join(lines)

    # ---- steps ---------------------------------------------------------------------------------
    def _ok(self, cmd, want: str | None = None, timeout: float = 180) -> tuple[bool, str]:
        rc, out = self.run(cmd, timeout)
        return rc == 0 and (want is None or want in out), out

    def find_container(self) -> str | None:
        ok, out = self._ok(self.find_container_cmd(), timeout=30)
        name = out.strip().splitlines()[0] if ok and out.strip() else None
        self.container = name if name and APP_UUID in name else None
        return self.container

    def preflight(self, allow_existing: bool = False) -> list:
        problems = []
        if not os.path.exists(os.path.join(self.data, "polybot.db")):
            problems.append(f"no local ledger at {self.data}/polybot.db")
        if not self.find_container():
            return problems + [f"no running container named {APP_UUID}-* on {self.host}"]
        for want, cmd in self.preflight_cmds():
            if want == "volume-empty" and allow_existing:
                continue
            ok, out = self._ok(cmd, want, timeout=60)
            if not ok:
                problems.append({"code-ok": f"{REMOTE_DATA} not mounted, or the deployed code has no MOVE_VERIFIED "
                                            f"supervisor (merge + deploy first)",
                                 "env-ok": "the container env is not set (POLYBOT_ON_SERVER=1, POLYBOT_DATA_DIR, "
                                           "the Polymarket keys, SUPABASE_URL): Coolify prep first",
                                 "volume-empty": f"{REMOTE_DATA}/polybot.db already exists on the server "
                                                 f"(a server ledger may be NEWER than the Mac's): pass "
                                                 f"--overwrite only if you know it is stale"}[want])
        return problems

    def _loop_gone(self) -> bool:
        deadline = self.clock() + STOP_WAIT_S
        while True:
            rc, out = self.run(["pgrep", "-f", LOOP_PATTERN], 15)
            if rc != 0 or not out.strip():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(3)

    def stop_mac(self) -> bool:
        for cmd in self.mac_stop_cmds():
            self.run(cmd, 30)                      # bootout of a stopped job is not an error worth stopping for
        return self._loop_gone()

    def start_mac(self) -> bool:
        ok = True
        for cmd in self.mac_start_cmds():
            rc, out = self.run(cmd, 30)
            ok = ok and (rc == 0 or "already" in out.lower())
        return ok

    def go(self, allow_existing: bool = False) -> int:
        problems = self.preflight(allow_existing)
        if problems:
            self.log("server_move go: NOT started — preflight:\n  " + "\n  ".join(problems))
            return 1
        self.log(f"preflight ok: container {self.container}")
        if not self.stop_mac():
            self.log("server_move go: the Mac loop did not stop — restarting it, nothing was moved")
            self.start_mac()
            return 1
        self.log("1 Mac loop stopped and disabled")
        start_wait = START_WAIT_S
        try:
            lease.release(lease.node_name())
            self.log("  Mac lease released")
        except Exception as exc:                   # the server then waits out the 10-min TTL: so do we
            start_wait = lease.LEASE_TTL_S + 3 * 60
            self.log(f"  lease release failed ({type(exc).__name__}): the server waits out the lease; "
                     f"so will this ({start_wait // 60:.0f} min)")
        try:
            m = migrate.snapshot(self.snap_dir(), data_dir=self.data)
            fp = m["fingerprint"]
            self.log(f"2 snapshot {self.snap_dir()}: {len(m['files'])} files, integrity {fp['integrity']}, "
                     f"gate_since_ts {fp['gate_since_ts']}, " +
                     ", ".join(f"{k} {v['decisions']}" for k, v in sorted(fp["modules"].items())))
            for cmd in self.copy_cmds():
                ok, out = self._ok(cmd, timeout=600)
                if not ok:
                    raise RuntimeError(f"copy failed: {out[-300:]}")
            self.log("3 copied into the volume")
            ok, out = self._ok(self.verify_cmd(), "verify: OK", timeout=300)
            if not ok or "marked:" not in out:
                raise RuntimeError(f"verify on the server failed:\n{out[-600:]}")
            self.log("4 verified on the server and marked")
            deadline = self.clock() + start_wait
            while True:
                rc, out = self.run(self.started_cmd(), 30)
                if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
                    break
                if self.clock() >= deadline:
                    raise RuntimeError(f"no 'polybot loop started on server' within {start_wait // 60:.0f} min")
                self.sleep(20)
            self.log("5 polybot loop started on server. The Mac loop stays disabled; "
                     "`python3 -m polybot.server_move rollback --yes` brings it back.")
            return 0
        except Exception as exc:
            self.log(f"server_move go: FAILED — {exc}\n  undoing: unmark the server, start the Mac loop")
            self.run(self.server_stop_cmd(), 60)
            self.log("  Mac loop " + ("restarted" if self.start_mac() else
                                      "did NOT restart: run launchctl enable + bootstrap by hand"))
            return 1

    def rollback(self) -> int:
        if not self.find_container():
            self.log(f"rollback: no running container named {APP_UUID}-* on {self.host}")
            return 1
        self.run(self.server_stop_cmd(), 60)
        deadline = self.clock() + STOP_WAIT_S
        while True:
            rc, out = self.run(self.ssh(self.dexec(f'pgrep -f "{LOOP_PATTERN}" || echo stopped')), 30)
            if "stopped" in out:
                break
            if self.clock() >= deadline:
                self.log("rollback: the server loop did not stop; the Mac loop was NOT started (two loops never)")
                return 1
            self.sleep(5)
        self.log("1 server loop stopped and unmarked")
        self.run(self.ssh(self.dexec(f"cd {REMOTE_APP} && python3 -m polybot.lease release --node server")), 60)
        back = self.snap_dir("back")
        rtmp = f"/tmp/{os.path.basename(back)}"
        for cmd in (self.ssh(self.dexec(f"cd {REMOTE_APP} && python3 -m polybot.migrate snapshot --out {rtmp}")),
                    self.ssh(f"docker cp {self.container}:{rtmp} {rtmp}"),
                    ["scp", "-r", *SSH_OPTS, f"{self.host}:{rtmp}", back]):
            ok, out = self._ok(cmd, timeout=600)
            if not ok:
                self.log(f"rollback: copying the server ledger back failed ({out[-300:]}); the Mac loop was NOT "
                         f"started. The server loop is stopped and unmarked; rerun rollback.")
                return 1
        self.log(f"2 server ledger copied back to {back}")
        for name in ("polybot.db",) + migrate.FILES:
            cur, new = os.path.join(self.data, name), os.path.join(back, name)
            if os.path.exists(cur):
                os.replace(cur, f"{cur}.pre-rollback-{self.stamp}")
            if os.path.exists(new):
                shutil.copy2(new, cur)
        problems = migrate.verify(back, data_dir=self.data)
        if problems:
            self.log("rollback: the copied-back ledger does not verify — the Mac loop was NOT started:\n  "
                     + "\n  ".join(problems))
            return 1
        self.log("3 verified on the Mac")
        ok = self.start_mac()
        self.log("4 Mac loop " + ("started" if ok else "did NOT start: run launchctl enable + bootstrap by hand"))
        return 0 if ok else 1

    def dry_run(self, keep: bool = False) -> int:
        """Every local step on a copy; the Mac loop keeps running and nothing leaves this machine."""
        import polybot_supervisor as sup
        t0 = self.clock()
        base = tempfile.mkdtemp(prefix="polybot-move-dryrun-")
        snap, vol = os.path.join(base, "snapshot"), os.path.join(base, "volume")
        try:
            m = migrate.snapshot(snap, data_dir=self.data)
            fp = m["fingerprint"]
            self.log(f"dry-run 2 snapshot: {len(m['files'])} files, "
                     f"{os.path.getsize(os.path.join(snap, 'polybot.db')) / 1e6:.0f} MB ledger, integrity "
                     f"{fp['integrity']}, gate_since_ts {fp['gate_since_ts']}")
            shutil.copytree(snap, vol)                                      # what docker cp does
            self.log("dry-run 3 copied into a stand-in volume")
            problems = migrate.verify(vol, data_dir=vol)
            if problems:
                self.log("dry-run 4 verify FAILED:\n  " + "\n  ".join(problems))
                return 1
            migrate.mark(vol, vol)
            self.log("dry-run 4 verify OK, marked")
            env = {"POLYBOT_ON_SERVER": "1", "POLYBOT_DATA_DIR": vol, "POLYMARKET_KEY_ID": "set",
                   "POLYMARKET_SECRET_KEY": "set", "SUPABASE_URL": "set", "SUPABASE_KEY": "set"}
            ok, why = sup.enabled(env)
            os.remove(os.path.join(vol, migrate.MARKER))
            blocked, blocked_why = sup.enabled(env)
            self.log(f"dry-run 5 the supervisor with this volume: {'READY' if ok else 'not ready: ' + why}; "
                     f"without the marker: {'READY (wrong!)' if blocked else 'waits (' + blocked_why.split(' — ')[0] + ')'}")
            if not ok or blocked:
                return 1
            self.log(f"dry-run done in {self.clock() - t0:.1f}s: the Mac loop was not touched and nothing left "
                     f"this machine. `go --yes` runs:\n{self.plan('go')}")
            return 0
        finally:
            if not keep:
                shutil.rmtree(base, ignore_errors=True)


def _load_env() -> None:
    """The Supabase keys for the lease step live in ~/second-brain/.env, which launchd sources and an
    interactive shell may not."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(config.ROOT)), ".env"))
    except Exception:
        pass


def main(argv=None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="polybot.server_move")
    ap.add_argument("cmd", choices=["plan", "dry-run", "go", "rollback"])
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--yes", action="store_true", help="go/rollback: actually do it (Alex said the words)")
    ap.add_argument("--overwrite", action="store_true", help="go: allow replacing a ledger already on the server")
    ap.add_argument("--keep", action="store_true", help="dry-run: keep the scratch copy")
    a = ap.parse_args(argv)
    mv = Move(a.host)
    if a.cmd == "plan":
        print(mv.plan("go"))
        print()
        print(mv.plan("rollback"))
        return 0
    if a.cmd == "dry-run":
        return mv.dry_run(keep=a.keep)
    if not a.yes:
        print(mv.plan(a.cmd))
        print(f"\n(not run: add --yes once Alex has said the words)")
        return 2
    return mv.go(allow_existing=a.overwrite) if a.cmd == "go" else mv.rollback()


if __name__ == "__main__":
    sys.exit(main())
