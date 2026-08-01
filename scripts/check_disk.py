#!/usr/bin/env python3
"""Early warning for the failure that has now cost two deploy outages: the box fills.

Both times (2026-07-2x and 2026-08-01) the disk was already unrecoverable before
anything said a word — the first symptom was a Coolify build dying on "No space
left on device", by which point Coolify's own Redis could no longer persist and
the queue wedged for 8.5 hours. The slope was visible for hours; nothing was
watching it.

This polls `/api/version` on both nodes (which now carries a `disk` block) and
escalates by band — PER NODE, because the same percentage means different things
on a build server and on a workstation (see NODES; the server's ladder is tighter):

    below notice   quiet   — healthy, prints one line, exits 0
    notice         prints, no event (don't train Alex to ignore rows)
    warning        system_event row, so it lands in the incident log + HUD
    critical       system_event row; on the server, builds are close to failing

Run it from launchd/cron hourly, or by hand:

    python3 scripts/check_disk.py            # both nodes
    python3 scripts/check_disk.py --quiet    # only print when something is wrong

Exit codes: 0 healthy/notice, 1 warning-or-worse, 2 a node was unreachable.
Fail-soft on reporting (never let a logging hiccup mask the disk reading itself).
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Thresholds are PER NODE, because the same percentage means different things.
#
# On the server, ~92% is where Docker builds start dying and Coolify's Redis stops
# persisting — that is the documented failure, so it warns early and hard.
#
# The Mac is a workstation and normally runs fuller (it sat at 93% the day this was
# written, with nothing wrong). Reusing the server's ladder there would file a
# CRITICAL on literally every run, and an alert that is always on is an alert nobody
# reads — which is how the original outage went unnoticed. What actually matters
# locally is headroom for the vault, the SQLite stores and whisper/ffmpeg scratch,
# so the local ladder sits higher and speaks only when that headroom is really gone.
NODES = [
    ("local", "http://127.0.0.1:5001/api/version", (88, 94, 97)),
    ("server", "https://clarvis.178.156.209.40.sslip.io/api/version", (75, 85, 92)),
]


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS, using certifi's roots when they're available.

    The python.org framework build on the Mac ships no CA bundle of its own, so a
    plain urlopen to the server fails CERTIFICATE_VERIFY_FAILED even though curl
    (system trust store) succeeds — which would make a perfectly healthy box read
    as UNREACHABLE forever. Verification stays ON; only the root source changes.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def probe(url: str, timeout: int = 15) -> tuple[dict | None, str]:
    """Fetch a node's /api/version.

    Returns (data, "") on success and (None, reason) otherwise. The reason is kept
    and printed because "the box is down" and "this Mac can't verify the cert" are
    completely different problems, and collapsing both to UNREACHABLE would send
    someone to SSH into a server that was fine all along.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed URLs above
                url, timeout=timeout, context=_ssl_context()) as r:
            return json.loads(r.read().decode("utf-8")), ""
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        return None, f"{type(e).__name__}: {reason}"


def report(level: str, message: str, detail: str = "") -> None:
    """Record a system_event so this surfaces in CLARVIS's incident log, not just a log
    file nobody opens. Fail-soft by design (report_event.py always exits 0)."""
    try:
        subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "report_event.py"),
             "disk-guard", level, message, detail],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        print(f"  (could not record event: {e})")


def band(pct: int, thresholds: tuple = (75, 85, 92)) -> str:
    notice, warning, critical = thresholds
    if pct >= critical:
        return "critical"
    if pct >= warning:
        return "warning"
    if pct >= notice:
        return "notice"
    return "ok"


def main() -> int:
    quiet = "--quiet" in sys.argv
    worst_exit = 0

    for name, url, thresholds in NODES:
        data, why = probe(url)
        if data is None:
            print(f"[{name}] UNREACHABLE — {url}\n           {why}")
            worst_exit = max(worst_exit, 2)
            continue

        disk = data.get("disk")
        if not disk:
            # A node running pre-disk-block code. Not an error — say so and move on,
            # so this script is safe to run during the window where only one node
            # has been deployed.
            if not quiet:
                print(f"[{name}] no disk block (running {data.get('commit', '?')}; "
                      f"pre-dates the disk field)")
            continue

        pct, free = disk.get("pct_used", 0), disk.get("free_gb", 0)
        level = band(pct, thresholds)
        line = (f"[{name}] disk {pct}% used, {free} GB free "
                f"of {disk.get('total_gb', '?')} GB — {level.upper()}")

        if level == "ok":
            if not quiet:
                print(line)
            continue

        print(line)
        if level == "notice":
            continue

        worst_exit = max(worst_exit, 1)
        report(level,
               f"{name} node disk at {pct}% ({free} GB free)",
               "Free space with: docker builder prune -af  (see NEEDS_ALEX.md §0a). "
               "Builds fail and Coolify's Redis stops persisting when this hits 100%.")

    return worst_exit


if __name__ == "__main__":
    sys.exit(main())
