"""
Tests for scripts/capability_watcher.py — the cheap trigger that replaced the
30-minute polling scheduled task.

Run directly:  python3 test_capability_watcher.py
No network, no Supabase, no Claude: the queue check and the Claude Code spawn are
both faked, so nothing here can file a request or start a build.

Covers:
  1. fire rules — spawns on a genuinely new request, stays silent on an empty
     queue, and does NOT re-spawn for a request already in_progress (which would
     restart a dying build every 2 minutes, forever)
  2. heartbeat — written on EVERY path including failures, since a dead watcher
     and a quiet queue are otherwise indistinguishable
  3. lock — a live PID blocks a second build; a stale/dead one self-heals
  4. queue-failure — an exception from Supabase is logged and exits non-zero
     rather than raising out of launchd
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import capability_watcher as w

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"{'PASS    ' if cond else '**FAIL**'} {label}")


def _sandbox():
    """Point every file the watcher touches at a temp dir."""
    d = tempfile.mkdtemp()
    w.HEARTBEAT = os.path.join(d, "hb")
    w.WATCHER_LOCK = os.path.join(d, "lock")
    w.LOG = os.path.join(d, "log")
    return d


def _run(queue, argv=("capability_watcher.py",)):
    """Run main() with a faked queue; returns (rc, spawned_count_or_None)."""
    spawned = []
    orig_pending, orig_spawn, orig_argv = w.pending, w.spawn_build, sys.argv
    w.pending = (queue if callable(queue) else (lambda: queue))
    w.spawn_build = lambda n: spawned.append(n)
    sys.argv = list(argv)
    try:
        rc = w.main()
    finally:
        w.pending, w.spawn_build, sys.argv = orig_pending, orig_spawn, orig_argv
    return rc, (spawned[0] if spawned else None)


def test_fire_rules():
    print("\n=== 1. when the watcher spawns a build ===")
    _sandbox()
    rc, n = _run([])
    check("empty queue → no build, clean exit", rc == 0 and n is None)

    _sandbox()
    rc, n = _run([{"slug": "a-1", "status": "pending"}])
    check("one new request → one build", rc == 0 and n == 1)

    _sandbox()
    rc, n = _run([{"slug": "a-1", "status": "pending"},
                  {"slug": "a-2", "status": "pending"}])
    check("two new requests → single build handling both", n == 2)

    _sandbox()
    rc, n = _run([{"slug": "a-1", "status": "in_progress"}])
    check("in_progress is NOT re-spawned (no 2-min restart loop)", rc == 0 and n is None)

    _sandbox()
    rc, n = _run([{"slug": "a-1", "status": "in_progress"},
                  {"slug": "a-2", "status": "pending"}])
    check("a new request alongside a stuck one still builds", n == 1)

    _sandbox()
    rc, n = _run([{"slug": "a-1", "status": "pending"}],
                 argv=("capability_watcher.py", "--dry-run"))
    check("--dry-run reports without spawning", rc == 0 and n is None)


def test_heartbeat_always():
    print("\n=== 2. heartbeat on every path ===")
    for label, queue in (("empty queue", []),
                         ("build path", [{"slug": "a", "status": "pending"}]),
                         ("queue failure", lambda: (_ for _ in ()).throw(RuntimeError("supabase down")))):
        _sandbox()
        _run(queue)
        check(f"heartbeat written — {label}",
              os.path.exists(w.HEARTBEAT) and os.path.getsize(w.HEARTBEAT) > 0)


def test_lock():
    print("\n=== 3. spawn lock ===")
    _sandbox()
    with open(w.WATCHER_LOCK, "w") as fh:
        fh.write(str(os.getpid()))          # our own pid == definitely alive
    rc, n = _run([{"slug": "a", "status": "pending"}])
    check("live build lock blocks a second spawn", rc == 0 and n is None)

    _sandbox()
    with open(w.WATCHER_LOCK, "w") as fh:
        fh.write("999999")                   # not a running process
    rc, n = _run([{"slug": "a", "status": "pending"}])
    check("dead pid → lock cleared and build proceeds",
          n == 1 and not os.path.exists(w.WATCHER_LOCK))

    _sandbox()
    with open(w.WATCHER_LOCK, "w") as fh:
        fh.write("not-a-pid")
    rc, n = _run([{"slug": "a", "status": "pending"}])
    check("garbage lock file self-heals", n == 1)


def test_queue_failure():
    print("\n=== 4. queue check failure ===")
    d = _sandbox()
    rc, n = _run(lambda: (_ for _ in ()).throw(RuntimeError("supabase down")))
    check("failure → rc=1, no build, no exception escapes", rc == 1 and n is None)
    check("failure is logged for the daily backstop to find",
          "supabase down" in open(w.LOG).read())


def test_single_source_of_truth():
    print("\n=== 5. guardrails not duplicated ===")
    src = open(w.__file__).read()
    check("spawn prompt points at the task SKILL.md rather than restating rules",
          "SKILL" in src and "follow it exactly" in src)
    check("open-request logic reuses capability_escalation, not a reimplementation",
          "pending_requests()" in src and "capability_request" not in src)


if __name__ == "__main__":
    test_fire_rules()
    test_heartbeat_always()
    test_lock()
    test_queue_failure()
    test_single_source_of_truth()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
