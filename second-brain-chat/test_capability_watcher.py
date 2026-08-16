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
  8. read retry — a transient DNS blip is absorbed silently (retry with backoff);
     only failures that survive all attempts surface
  9. failure streak — consecutive failed polls persist a count; a success clears
     it; hitting the alert threshold logs a LOUD line, because the heartbeat
     stays fresh through a network outage and would otherwise hide it
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
    w.FAILSTREAK = os.path.join(d, "failstreak")
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
          "esc.pending_requests" in src and "capability_request" not in src)


def test_auth_failure_is_loud():
    """The CLI exits 0 when it can't authenticate, so rc proves nothing.

    Regression for Aug 9-14 2026: five days of dead builds logged as
    'build finished rc=0' while the queue silently went undrained.
    """
    print("\n=== 6. auth failure cannot hide behind rc=0 ===")
    _sandbox()

    class P:
        def __init__(self, out="", err="", rc=0):
            self.stdout, self.stderr, self.returncode = out, err, rc

    check("the exact rc=0 failure that hid for 5 days is caught",
          w.check_auth_failure(
              P("Failed to authenticate: OAuth session expired and could not "
                "be refreshed", rc=0)) is True)
    check("logged-out variant caught", w.check_auth_failure(P("Not logged in · Please run /login")) is True)
    check("failure on stderr caught too", w.check_auth_failure(P("", "Invalid API key")) is True)
    check("a normal build is NOT flagged",
          w.check_auth_failure(P("Shipped the fix; all suites green.")) is False)

    logged = open(w.LOG).read()
    check("log names it AUTH FAILURE and says the queue isn't draining",
          "AUTH FAILURE" in logged and "NOT being drained" in logged)
    check("log tells the reader how to fix it", "claude auth" in logged)


def test_token_reaches_the_child():
    """launchd gives the watcher a bare env, so the token must be passed in."""
    print("\n=== 7. credentials reach the spawned CLI ===")
    d = _sandbox()
    orig_root, orig_chat = w.ROOT, w.CHAT
    try:
        w.ROOT = w.CHAT = d
        with open(os.path.join(d, ".env"), "w") as fh:
            fh.write("SUPABASE_URL=https://example.test\n"
                     "CLAUDE_CODE_OAUTH_TOKEN=sk-test-not-a-real-token\n")
        env = w.build_env()
        check("token from .env is handed to the child process",
              env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-test-not-a-real-token")
        check("rest of the environment is preserved", "PATH" in env)

        # no token configured → inherit untouched, so an interactive login still works
        with open(os.path.join(d, ".env"), "w") as fh:
            fh.write("SUPABASE_URL=https://example.test\n")
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        check("absent token leaves the environment alone (CLI uses its own login)",
              "CLAUDE_CODE_OAUTH_TOKEN" not in w.build_env())
    finally:
        w.ROOT, w.CHAT = orig_root, orig_chat

    src = open(w.__file__).read()
    check("the spawn actually uses build_env(), not a bare inherit",
          "env=build_env()" in src)


def test_read_retry():
    """[Errno 8] DNS blips clear in seconds — one poll should absorb them."""
    print("\n=== 8. transient read failures are retried ===")
    _sandbox()
    naps = []
    orig_sleep = w.time.sleep
    w.time.sleep = naps.append
    try:
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(8, "nodename nor servname provided, or not known")
            return [{"slug": "a", "status": "pending"}]

        got = w._read_with_retry(flaky)
        check("two blips then success → result returned, nothing raised",
              got == [{"slug": "a", "status": "pending"}] and calls["n"] == 3)
        check("backoff grows between attempts", naps == [w.READ_BACKOFF_S,
                                                         w.READ_BACKOFF_S * 2])
        check("a blip that clears is NOT logged (log stays readable)",
              not os.path.exists(w.LOG))

        naps.clear()
        dead_calls = {"n": 0}

        def dead():
            dead_calls["n"] += 1
            raise OSError(8, "nodename nor servname provided, or not known")

        try:
            w._read_with_retry(dead)
            raised = False
        except OSError:
            raised = True
        check("a real outage still raises after all attempts",
              raised and dead_calls["n"] == w.READ_ATTEMPTS)
        check("no sleep after the final attempt (fail fast once decided)",
              len(naps) == w.READ_ATTEMPTS - 1)
    finally:
        w.time.sleep = orig_sleep


def test_fail_streak():
    """The heartbeat stays fresh through a network outage, so the streak file is
    the ONLY signal separating 'queue quiet' from 'queue unreachable'."""
    print("\n=== 9. failure streak: blips stay quiet, outages get loud ===")
    _sandbox()
    boom = lambda: (_ for _ in ()).throw(OSError(8, "nodename nor servname"))

    _run(boom)
    _run(boom)
    check("consecutive failures persist a growing count across runs",
          w._read_fail_streak() == 2)
    check("streak file records when the outage started",
          len(open(w.FAILSTREAK).read().splitlines()) == 2)
    logged = open(w.LOG).read()
    check("below the threshold: logged, but no siren", "!!" not in logged
          and "streak 2" in logged)

    _run([])
    check("one successful poll clears the streak",
          not os.path.exists(w.FAILSTREAK) and w._read_fail_streak() == 0)

    _sandbox()
    for _ in range(w.FAIL_STREAK_ALERT):
        _run(boom)
    logged = open(w.LOG).read()
    check("threshold crossed → loud UNREACHABLE line",
          "!!" in logged and "UNREACHABLE" in logged)
    check("the loud line explains why nothing else caught it",
          "heartbeat stays fresh" in logged)
    check("siren fires once at the threshold, not every poll after",
          logged.count("!!") == 1)

    _run([])
    check("recovery after an alert-level outage is logged",
          "recovered after" in open(w.LOG).read())

    _sandbox()
    with open(w.FAILSTREAK, "w") as fh:
        fh.write("garbage\n")
    _run(boom)
    check("corrupt streak file self-heals instead of crashing the watcher",
          w._read_fail_streak() == 1)


if __name__ == "__main__":
    test_fire_rules()
    test_heartbeat_always()
    test_lock()
    test_queue_failure()
    test_single_source_of_truth()
    test_auth_failure_is_loud()
    test_token_reaches_the_child()
    test_read_retry()
    test_fail_streak()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
