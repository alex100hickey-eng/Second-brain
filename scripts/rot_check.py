#!/usr/bin/env python3
"""
rot_check.py — is anything in the school/CLARVIS system quietly rotting?

The 2026-09-02 audit found ten days of decay nobody had noticed: pace stuck at its
baseline, empty capture logs, a ranked day full of stale items, tests writing to
production. Every one of those is visible in data. This prints a short report —
⚠ lines are things to act on, ✓ lines are fine — from the vault CSVs and
Supabase, with no model call. The Friday sweep runs it and repeats the ⚠ lines;
`--notify` pushes them to the phone.

    python3 scripts/rot_check.py [--notify]
"""

import csv
import json
import ssl
import os
import re
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TZ = ZoneInfo("America/New_York")
VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
SCHOOL = os.path.join(VAULT, "School")
DONE = {"submitted", "graded", "done", "complete", "completed"}
SERVER = "https://clarvis.178.156.209.40.sslip.io"
WARN, OK = [], []


def warn(s):
    WARN.append(s)


def ok(s):
    OK.append(s)


def _env():
    envp = os.path.join(ROOT, ".env")
    if os.path.exists(envp):
        for line in open(envp):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _rows(name):
    try:
        with open(os.path.join(SCHOOL, name), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return None


def check_vault(today):
    courses = _rows("courses.csv")
    if courses is None:
        warn("School/courses.csv unreadable")
        return
    stale = []
    for c in courses:
        if int((c.get("lead_target_days") or "0") or 0) <= 0:
            continue
        try:
            pt = datetime.strptime((c.get("prepared_through") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            stale.append(f"{c.get('course')} (unset)")
            continue
        if (today - pt).days > 9:
            stale.append(f"{c.get('course')} ({(today - pt).days}d old)")
    (warn if stale else ok)(f"prepared_through untouched >9d: {', '.join(stale)}" if stale
                            else "prepared_through moved within 9 days for every course")
    asg = _rows("assignments.csv") or []

    def _ungraded(r):
        # weight_pct 0 = Canvas not_graded prep (ACCT "Day N Reading"): nothing
        # to submit, so it stays "open" forever by design — not a sync failure.
        try:
            return float((r.get("weight_pct") or "").strip()) == 0
        except ValueError:
            return False
    past_open = [r for r in asg if (r.get("status") or "").lower() not in DONE
                 and not _ungraded(r)
                 and (r.get("due_date") or "")[:10] < (today - timedelta(days=2)).isoformat()
                 and (r.get("due_date") or "")]
    (warn if past_open else ok)(
        f"{len(past_open)} assignment row(s) open >2d past due (nightly sync not flipping them?): "
        + "; ".join(f"{r['course']} {r['title'][:30]}" for r in past_open[:4])
        if past_open else "no assignment rows sitting open past their due date")
    graded = sum(1 for r in asg if re.match(r"^\s*[0-9.]+\s*/\s*[0-9.]+", r.get("grade") or ""))
    rl = _rows("review-log.csv") or []
    (ok if graded or len(rl) else warn)(
        f"capture: {graded} graded rows, {len(rl)} review-log rows"
        + ("" if graded or rl else " — every capture loop is still empty"))
    for name in ("Weekend Map — Fall 2026.md",):
        p = os.path.join(SCHOOL, name)
        if os.path.exists(p):
            age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(p))).days
            (warn if age > 8 else ok)(f"{name} last touched {age}d ago")


def check_supabase(now_utc):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    except Exception as e:
        warn(f"Supabase unreachable: {str(e)[:80]}")
        return
    T = "Agent Outputs"
    since7 = (now_utc - timedelta(days=7)).isoformat()

    def q(agent, limit=400, since=None):
        qq = sb.table(T).select("id,output_text,created_at").eq("agent_name", agent)
        if since:
            qq = qq.gte("created_at", since)
        return qq.order("id", desc=True).limit(limit).execute().data or []

    new = []
    for r in q("intake_event"):
        try:
            j = json.loads(r["output_text"])
        except Exception:
            continue
        if j.get("status", "new") == "new":
            new.append(r["created_at"][:10])
    (warn if len(new) > 40 else ok)(f"intake backlog: {len(new)} new"
                                    + (f", oldest {min(new)}" if new else ""))
    nud = Counter()
    for r in q("jarvis_nudge", since=since7):
        try:
            nud[(json.loads(r["output_text"]).get("key") or "").split(":")[0]] += 1
        except Exception:
            pass
    total = sum(nud.values())
    (warn if total > 70 else ok)(f"nudges last 7d: {total} ({', '.join(f'{k} {v}' for k, v in nud.most_common(6))})")
    errs = Counter()
    for r in q("system_event", since=since7):
        try:
            j = json.loads(r["output_text"])
        except Exception:
            continue
        if j.get("level") in ("error", "critical"):
            errs[f"{j.get('component')}: {str(j.get('message'))[:40]}"] += 1
    (warn if errs else ok)("errors last 7d: " + ("; ".join(f"{k} ×{v}" for k, v in errs.most_common(5))
                                                if errs else "none"))
    stale = []
    seen = set()
    for r in q("intake_state", limit=200):
        try:
            j = json.loads(r["output_text"])
        except Exception:
            continue
        k = j.get("key", "")
        if not k.startswith("heartbeat:") or k in seen:
            continue
        seen.add(k)
        try:
            age = (datetime.now(LOCAL_TZ) - datetime.fromisoformat(j["beat_at"])).total_seconds()
            if age > float(j.get("stale_after_s") or 0):
                stale.append(f"{k[10:]} ({age / 3600:.0f}h)")
        except Exception:
            pass
    (warn if stale else ok)("stale heartbeats: " + (", ".join(stale) if stale else "none"))
    sc = 0
    for r in q("intake_state", limit=200):
        try:
            j = json.loads(r["output_text"])
        except Exception:
            continue
        if j.get("key") == "orders:scorecard":
            sc = sum(1 for d in (j.get("days") or {})
                     if d >= (datetime.now(LOCAL_TZ).date() - timedelta(days=7)).isoformat())
    (ok if sc else warn)(f"scorecard days logged last 7d: {sc}")


def check_nodes():
    # The Mac's Python has no CA bundle wired into urllib's default context, so
    # every HTTPS probe here raised CERTIFICATE_VERIFY_FAILED and this check
    # reported the server dead on every single run while it was serving fine.
    # A health check that cries wolf daily is worse than no health check.
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _CTX = ssl.create_default_context()

    def ver(url):
        for _ in range(2):                      # one retry: the first hit can be slow
            try:
                kw = {"context": _CTX} if url.startswith("https") else {}
                with urllib.request.urlopen(url, timeout=15, **kw) as r:
                    return json.load(r).get("commit", "?")
            except Exception:
                continue
        return None
    srv, loc = ver(SERVER + "/api/version"), ver("http://localhost:5001/api/version")

    def _rev(ref):
        try:
            return subprocess.run(["git", "rev-parse", "--short=12", ref], cwd=ROOT,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return "?"
    head = _rev("HEAD")
    # Coolify deploys origin/main. Comparing the server to LOCAL head called a
    # healthy deploy "pending" for as long as anything sat unpushed — and right
    # now eleven commits do.
    pushed = _rev("origin/main") or head
    if srv is None:
        warn("server /api/version unreachable")
    elif srv != pushed:
        warn(f"server runs {srv}, origin/main is {pushed} — deploy pending or failed")
    else:
        ok(f"server on origin/main {pushed}")
    if head and pushed and head != pushed:
        n = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip() or "?"
        warn(f"{n} local commit(s) never pushed — the server cannot be running them")
    if loc is None:
        warn("local node not answering on :5001")
    elif loc != head:
        warn(f"local node runs {loc}, HEAD is {head}")
    else:
        ok("local node on HEAD")



# ---------------------------------------------------------------------------
# Are the background jobs that make money actually loaded?
#
# On 2026-09-19 the Splitframe reply watcher had been dead for two days: its plist had been renamed
# `.plist.disabled`, so launchctl had no row for it at all. Nothing caught it, because a dead job
# and a quiet job produce identical evidence — the last log line read "no prospect replies", which
# is exactly what a healthy run writes. 46 cold emails were out with nothing watching for answers.
#
# So this checks two separate things, because either alone can be true while the job is broken:
#   1. launchctl still knows the label  (catches disabled/unloaded/never-installed)
#   2. the job's log has moved recently (catches loaded-but-erroring, or running stale code)
# ---------------------------------------------------------------------------

# label -> (log filename, how many hours of silence is abnormal)
MONEY_JOBS = {
    "com.secondbrain.replywatch": ("reply_watch.log", 2),
    "com.secondbrain.splitframesend": ("splitframe_send.log", 24),
    "com.secondbrain.capabilitywatcher": ("capability_watcher.log", 2),
    "com.secondbrain.kickscan": ("kick_scan.log", 26),
}


def _loaded_labels() -> set:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return set()
    return {ln.split("\t")[-1].strip() for ln in out.splitlines()[1:] if ln.strip()}


def check_jobs(now=None):
    now = now or datetime.now(LOCAL_TZ)
    loaded = _loaded_labels()
    if not loaded:
        warn("launchctl list returned nothing — cannot tell whether any money job is alive")
        return
    for label, (logname, max_quiet_h) in sorted(MONEY_JOBS.items()):
        short = label.replace("com.secondbrain.", "")
        if label not in loaded:
            warn(f"{short}: NOT LOADED — launchctl has no such job (check for a .plist.disabled)")
            continue
        path = os.path.join(ROOT, "scripts", logname)
        try:
            age_h = (now.timestamp() - os.path.getmtime(path)) / 3600.0
        except OSError:
            warn(f"{short}: loaded, but its log {logname} is missing — has it ever run?")
            continue
        if age_h > max_quiet_h:
            warn(f"{short}: loaded but silent for {age_h:.1f}h (expected within {max_quiet_h}h)")
        else:
            ok(f"{short}: alive, last wrote {age_h:.1f}h ago")


def main():
    _env()
    today = datetime.now(LOCAL_TZ).date()
    check_vault(today)
    check_supabase(datetime.now(timezone.utc))
    check_nodes()
    check_jobs()
    print(f"ROT CHECK — {today}")
    for s in WARN:
        print("  ⚠", s)
    for s in OK:
        print("  ✓", s)
    if "--notify" in sys.argv and WARN:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if topic:
            body = "\n".join(f"⚠ {s}" for s in WARN[:6])
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(),
                                         headers={"Title": f"Rot check: {len(WARN)} thing(s)",
                                                  "Tags": "wrench"})
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
