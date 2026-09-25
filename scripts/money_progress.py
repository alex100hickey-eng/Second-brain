#!/usr/bin/env python3
"""money_progress.py — the daily scorecard for the money lanes.

Reads the ledgers (prospect tracker, Mac send log, clipbot ledger, polybot report,
NEEDS_ALEX, Shift Log, Revenue.csv), scores the day against the targets in
`Money/ROADMAP — money lanes (2026-09-24).md`, and writes:

  Money/PROGRESS.md          today's scorecard (overwritten)
  Money/progress.csv         one row per day (appended / today's row replaced)
  Money/progress-line.txt    one line for the morning brief

Cash is read from Revenue.csv only. Nothing here asks a model anything.

    python3 scripts/money_progress.py            # write
    python3 scripts/money_progress.py --print    # write and print PROGRESS.md
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "second-brain-chat"))

VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
MONEY = os.path.join(VAULT, "Money")
TRACKER = os.path.join(MONEY, "prospect-tracker.csv")
REVENUE = os.path.join(MONEY, "Revenue.csv")
SEND_LOG = os.path.join(ROOT, "scripts", "splitframe_send.launchd.log")
SEND_CORRECTIONS = os.path.join(ROOT, "scripts", "splitframe_send_corrections.log")
CLIPBOT_DB = os.path.join(ROOT, "second-brain-chat", "clipbot", "clipbot.db")
POLYBOT_LOG = os.path.join(ROOT, "second-brain-chat", "polybot", "loop.log")
NEEDS_ALEX = os.path.join(ROOT, "NEEDS_ALEX.md")
SHIFT_LOG = os.path.join(MONEY, "Shift Log.md")
PROGRESS_MD = os.path.join(MONEY, "PROGRESS.md")
PROGRESS_CSV = os.path.join(MONEY, "progress.csv")
PROGRESS_LINE = os.path.join(MONEY, "progress-line.txt")
PROGRESS_JSON = os.path.join(MONEY, "progress.json")

TARGETS = {
    "sf_first_touches": 10,     # named first touches a day (the cap)
    "creator_sends": 5,
    "clip_posts_per_account": 2,
}
SUBMIT_WINDOW_S = 30 * 60


VAULT_GIT = os.path.expanduser("~/.second-brain-vault.git")
SF_DATALESS = 0x40000000


def _mirror_read(path: str) -> str:
    """The vault git mirror's copy of a vault file, or "" when it has none."""
    rel = os.path.relpath(path, VAULT)
    if rel.startswith(".."):
        return ""
    try:
        r = subprocess.run(["git", "--git-dir", VAULT_GIT, "show", f"HEAD:{rel}"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ""
    except Exception:                               # noqa: BLE001
        return ""


def _read(path: str) -> str:
    """A vault file evicted by iCloud (dataless) would block or read empty; the git
    mirror always has the last synced copy, so read that instead of waiting."""
    try:
        if path.startswith(VAULT) and os.stat(path).st_flags & SF_DATALESS:
            return _mirror_read(path)
    except (OSError, AttributeError):
        pass
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _mirror_read(path) if path.startswith(VAULT) else ""


def _c(v) -> str:
    return (v or "").strip()


def _dt(s: str):
    s = _c(s)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ tracker + send log
def tracker_rows() -> list:
    txt = _read(TRACKER)
    return list(csv.DictReader(io.StringIO(txt))) if txt else []


def is_creator(row: dict) -> bool:
    try:
        import funnel_report                       # type: ignore
        return funnel_report.lane(row) == "creator"
    except Exception:                              # noqa: BLE001
        blob = (_c(row.get("category")) + " " + _c(row.get("notes")) + " " + _c(row.get("signal"))).lower()
        return "creator" in blob or "twitch" in blob or "streamer" in blob


SENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) item \d+: SENT to (\S+) .*?(\[follow-up\])?\s*$")


def sends_from_log() -> list:
    """[(date, time, address, is_followup)] from the Mac sender log plus the corrections log
    (sends the log missed, recorded by hand in the same format)."""
    out = []
    for path in (SEND_LOG, SEND_CORRECTIONS):
        for line in _read(path).splitlines():
            m = SENT_RE.match(line.strip())
            if m:
                out.append((date.fromisoformat(m.group(1)), m.group(2), m.group(3).lower(), bool(m.group(4))))
    return out


def log_start(sends: list):
    """Sends before the log existed are unknown, not overdue."""
    return min((s[0] for s in sends), default=None)


def row_addresses(row: dict) -> set:
    addrs = set()
    for k in ("email", "email_generic", "email_alternates"):
        for a in re.split(r"[;,\s]+", _c(row.get(k))):
            if "@" in a:
                addrs.add(a.lower())
    return addrs


def splitframe_metrics(rows: list, sends: list, today: date) -> dict:
    dtc = [r for r in rows if not is_creator(r)]
    sent = [r for r in dtc if _dt(r.get("sent_date"))]
    creator_addrs = set().union(*[row_addresses(r) for r in rows if is_creator(r)]) if rows else set()
    log_today = [s for s in sends if s[0] == today and s[2] not in creator_addrs]
    log_7d = [s for s in sends if today - timedelta(days=6) <= s[0] <= today and s[2] not in creator_addrs]
    first_today = [s for s in log_today if not s[3]]
    fu_today = [s for s in log_today if s[3]]
    # overdue follow-ups: a follow-up date strictly before today with no send to the row after it
    overdue, unknown = [], []
    start = log_start(sends)
    for r in sent:
        if _c(r.get("replied")) or _c(r.get("outcome")):
            continue
        addrs = row_addresses(r)
        for k in ("followup1_date", "followup2_date"):
            d = _dt(r.get(k))
            if not d or d >= today or any(s[0] >= d and s[2] in addrs for s in sends):
                continue
            (unknown if start and d < start else overdue).append((r.get("brand"), k, d.isoformat()))
    named_sent = sum(1 for r in sent if _c(r.get("contact_name")))
    replies = [r for r in sent if _c(r.get("replied"))]
    calls = [r for r in sent if _c(r.get("call_date"))]
    closes = [r for r in sent if re.search(r"won|signed|paid|closed", _c(r.get("outcome")), re.I)]
    return dict(
        sent_total=len(sent), named_sent=named_sent, replies=len(replies), calls=len(calls), closes=len(closes),
        first_today=len(first_today), fu_today=len(fu_today), sends_7d=len(log_7d),
        overdue=overdue, unknown=unknown, queue_qualified=sum(1 for r in dtc if _c(r.get("status")) == "qualified" and not _dt(r.get("sent_date"))),
    )


def creator_metrics(rows: list, sends: list, today: date) -> dict:
    cre = [r for r in rows if is_creator(r)]
    addrs = set().union(*[row_addresses(r) for r in cre]) if cre else set()
    sent = [r for r in cre if _dt(r.get("sent_date"))]
    log_today = [s for s in sends if s[0] == today and s[2] in addrs]
    return dict(
        prospects=len(cre), sent_total=len(sent), sends_today=len(log_today),
        replies=sum(1 for r in sent if _c(r.get("replied"))),
        closes=sum(1 for r in sent if re.search(r"won|signed|paid", _c(r.get("outcome")), re.I)),
        bounced=sum(1 for r in sent if "bounce" in _c(r.get("outcome")).lower()),
    )


# ------------------------------------------------------------------ clipbot
def clipping_metrics(today: date) -> dict:
    out = dict(posts_today=0, accounts_posting=0, submitted_in_window=0, submitted_late=0, unsubmitted=0,
               views_total=0, usd_approved=0.0, usd_settled=0.0, posts_total=0, linked_accounts=0, error="")
    try:
        con = sqlite3.connect(f"file:{CLIPBOT_DB}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        start = time.mktime(datetime(today.year, today.month, today.day).timetuple())
        rows = cur.execute("select posted_at, submitted_at, views, usd_approved, usd_settled, account from posts").fetchall()
        out["posts_total"] = len(rows)
        accts = set()
        for posted, submitted, views, appr, settled, acct in rows:
            out["views_total"] += int(views or 0)
            out["usd_approved"] += float(appr or 0)
            out["usd_settled"] += float(settled or 0)
            if posted and posted >= start:
                out["posts_today"] += 1
                accts.add(acct)
            if submitted:
                if posted and submitted - posted <= SUBMIT_WINDOW_S:
                    out["submitted_in_window"] += 1
                else:
                    out["submitted_late"] += 1
            else:
                out["unsubmitted"] += 1
        out["accounts_posting"] = len(accts)
        try:
            cols = [c[1] for c in cur.execute("pragma table_info(accounts)").fetchall()]
            if "linked" in cols:
                # `linked` is TEXT ("whop"); a bare `where linked` casts it to 0 in SQLite and counts nothing.
                out["linked_accounts"] = cur.execute(
                    "select count(*) from accounts where coalesce(linked, '') != ''").fetchone()[0]
            elif "verified" in cols:
                out["linked_accounts"] = cur.execute("select count(*) from accounts where verified").fetchone()[0]
        except sqlite3.Error:
            pass
        out["campaigns"] = _clip_payout_by_campaign(cur)
        con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)[:80]
    return out


CLIP_PAYOUT_FLOOR = {"vyro": 5000, "whop": 1000}   # per-post floor; mirrors clipbot.ledger.PAYOUT_FLOOR_VIEWS


def _clip_payout_by_campaign(cur) -> list:
    """Per live campaign: submitted posts, views since submission, estimate at the campaign's rate (posts
    past the board's floor only), and posts that can't be submitted because the account isn't linked.
    Same arithmetic as clipbot's `payout_by_campaign`, read-only SQL so this script never imports clipbot."""
    pcols = {c[1] for c in cur.execute("pragma table_info(posts)").fetchall()}
    base = "p.views_at_submit" if "views_at_submit" in pcols else "0"
    try:
        linked = {h: (l or "").split(",") for h, l in cur.execute("select handle, linked from accounts").fetchall()}
    except sqlite3.Error:
        linked = {}
    rows = cur.execute(f"""select k.name, lower(k.marketplace), k.rate_per_1k, p.views, {base}, p.submitted_at,
                                  coalesce(p.account, '')
                           from posts p join variants v on v.id = p.variant_id join clips c on c.id = v.clip_id
                           join sources s on s.id = c.source_id join campaigns k on k.id = s.campaign_id
                           where k.status = 'active'""").fetchall()
    camps = {}
    for name, board, rate, views, at_submit, submitted, acct in rows:
        c = camps.setdefault(name, dict(campaign=name, board=board, rate=float(rate or 0), submitted=0,
                                        views_since_submit=0, usd_est=0.0, blocked_unlinked=0, blocked_views=0))
        if submitted:
            since = max(0, int(views or 0) - int(at_submit or 0))
            c["submitted"] += 1
            c["views_since_submit"] += since
            if since >= CLIP_PAYOUT_FLOOR.get(board, 0):
                c["usd_est"] += since / 1000.0 * c["rate"]
        elif board not in linked.get(acct, []):
            c["blocked_unlinked"] += 1
            c["blocked_views"] += int(views or 0)
    return sorted(camps.values(), key=lambda c: (-c["usd_est"], c["campaign"]))


# ------------------------------------------------------------------ polybot
GATE_RE = re.compile(r"^\s*gate (\w+)\s+(hold|PASS)\s*[—-]\s*(.*)$")


def polybot_metrics() -> dict:
    out = dict(alive=False, gates={}, passing=[], etas={}, accrual="", error="")
    try:
        age = time.time() - os.path.getmtime(POLYBOT_LOG)
        out["alive"] = age < 15 * 60
        out["log_age_min"] = int(age // 60)
    except OSError:
        out["log_age_min"] = -1
    try:
        r = subprocess.run([sys.executable, "-m", "polybot.runner", "report", "--days", "1"],
                           capture_output=True, text=True, timeout=90,
                           cwd=os.path.join(ROOT, "second-brain-chat"))
        out["etas"], out["accrual"] = {}, ""
        last = None
        for line in r.stdout.splitlines():
            m = GATE_RE.match(line)
            if m:
                last = m.group(1)
                out["gates"][last] = m.group(3).strip()
                if m.group(2) == "PASS":
                    out["passing"].append(last)
                continue
            e = re.match(r"^\s*eta (.*)$", line)
            if e and last:
                out["etas"][last] = e.group(1).strip()
                continue
            if "incentive accrual" in line:
                out["accrual"] = line.strip()
    except Exception as exc:                        # noqa: BLE001
        out["error"] = str(exc)[:80]
    return out


# ------------------------------------------------------------------ blockers, shifts, cash
def blockers() -> dict:
    txt = _read(NEEDS_ALEX)
    section = txt.split("## 💰 Money lanes", 1)[-1].split("\n---", 1)[0] if "Money lanes" in txt else txt
    dated = re.findall(r"^\*\*(\d{4}-\d{2}-\d{2})", section, re.M)
    numbered = re.findall(r"^### (\d+)\. (?!RESOLVED)(.+)$", section, re.M)
    try:
        updated = date.fromtimestamp(os.path.getmtime(NEEDS_ALEX))
    except OSError:
        updated = None
    return dict(dated=len(dated), numbered=[n[1][:60] for n in numbered], updated=updated)


def shifts_today(today: date) -> int:
    return len(re.findall(rf"^## {today.isoformat()}", _read(SHIFT_LOG), re.M))


def cash(today: date) -> dict:
    mtd = total = 0.0
    txt = _read(REVENUE)
    for r in csv.DictReader(io.StringIO(txt)) if txt else []:
        if _c(r.get("status")).lower() != "paid":
            continue
        try:
            amt = float(_c(r.get("amount_usd")) or 0)
        except ValueError:
            continue
        total += amt
        d = _dt(r.get("date"))
        if d and d.year == today.year and d.month == today.month:
            mtd += amt
    return dict(mtd=mtd, total=total)


# ------------------------------------------------------------------ activity feed
def _git_events(today: date) -> list:
    try:
        r = subprocess.run(["git", "log", "--since", today.isoformat() + " 00:00", "--format=%ct|%s"],
                           capture_output=True, text=True, timeout=20, cwd=ROOT)
    except Exception:                               # noqa: BLE001
        return []
    out = []
    for line in r.stdout.splitlines():
        ts, _, msg = line.partition("|")
        if ts.isdigit():
            out.append(dict(t=int(ts), lane="all", kind="code", text=msg[:120]))
    return out


def _send_events(sends: list, rows: list, today: date) -> list:
    brand_by_addr = {}
    for r in rows:
        for a in row_addresses(r):
            brand_by_addr[a] = _c(r.get("brand")) or a
    out = []
    for d, hm, addr, fu in sends:
        if d != today:
            continue
        t = int(time.mktime(datetime.strptime(f"{d.isoformat()} {hm}", "%Y-%m-%d %H:%M").timetuple()))
        who = brand_by_addr.get(addr, addr)
        out.append(dict(t=t, lane="B" if is_creator(next((r for r in rows if addr in row_addresses(r)), {})) else "A",
                        kind="send", text=f"{'follow-up' if fu else 'first touch'} sent to {who}"))
    return out


def _clip_events(today: date) -> list:
    out = []
    try:
        con = sqlite3.connect(f"file:{CLIPBOT_DB}?mode=ro", uri=True, timeout=5)
        start = time.mktime(datetime(today.year, today.month, today.day).timetuple())
        for posted, submitted, platform, acct, url in con.execute(
                "select posted_at, submitted_at, platform, account, url from posts where posted_at >= ? or submitted_at >= ?",
                (start, start)):
            if posted and posted >= start:
                out.append(dict(t=int(posted), lane="C", kind="post", text=f"posted {platform} on {acct}", url=url))
            if submitted and submitted >= start:
                out.append(dict(t=int(submitted), lane="C", kind="submit", text=f"submitted {platform} ({acct}) to Whop", url=url))
        con.close()
    except sqlite3.Error:
        pass
    return out


def _shift_events(today: date) -> list:
    out = []
    for m in re.finditer(rf"^## {today.isoformat()}\s*~?(\d{{1,2}}:\d{{2}})?[^\n]*?(?:—|-)\s*([^\n]+)$", _read(SHIFT_LOG), re.M):
        hm = m.group(1) or "00:00"
        try:
            t = int(time.mktime(datetime.strptime(f"{today.isoformat()} {hm}", "%Y-%m-%d %H:%M").timetuple()))
        except ValueError:
            continue
        out.append(dict(t=t, lane="all", kind="shift", text=m.group(2).strip()[:120]))
    return out


def _file_events(today: date) -> list:
    out = []
    start = time.mktime(datetime(today.year, today.month, today.day).timetuple())
    for path, text in ((NEEDS_ALEX, "blockers list updated"),
                       (os.path.join(ROOT, "scripts", "offer_statics.launchd.log"), "statics backstop ran"),
                       (os.path.join(MONEY, "Named Contacts.csv"), "founder-name research updated")):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if mt >= start:
            out.append(dict(t=int(mt), lane="A" if "Named" in path or "statics" in path else "all", kind="file", text=text))
    for name in os.listdir(MONEY) if os.path.isdir(MONEY) else []:
        if name.startswith("prospect-tracker.csv.bak-"):
            try:
                mt = os.path.getmtime(os.path.join(MONEY, name))
            except OSError:
                continue
            if mt >= start:
                out.append(dict(t=int(mt), lane="A", kind="tracker", text="tracker written: " + name.split(".bak-", 1)[1][:40]))
    return out


def activity(sends: list, rows: list, today: date, prev_gates: str, gates: dict) -> list:
    ev = _git_events(today) + _send_events(sends, rows, today) + _clip_events(today) + _shift_events(today) + _file_events(today)
    if prev_gates:
        prev = dict(p.split(":", 1) for p in prev_gates.split() if ":" in p)
        for k, v in gates.items():
            now = v.split(" ")[0]
            if k in prev and prev[k] != now:
                ev.append(dict(t=int(time.time()), lane="D", kind="gate", text=f"{k} gate {prev[k]} -> {now}"))
    ev.sort(key=lambda e: e["t"], reverse=True)
    return ev[:80]


# ------------------------------------------------------------------ stages
def stages(sf: dict, cr: dict, cl: dict, pb: dict, money: dict) -> dict:
    a = 0
    if sf["replies"]:
        a = 1
    if sf["calls"]:
        a = max(a, 1)
    if sf["closes"] or money["total"] >= 650:
        a = 2
    if sf["closes"] >= 3:
        a = 3
    b = 0 if not cr["replies"] else 1
    if cr["closes"]:
        b = 2
    c = 0
    if cl["submitted_in_window"] or cl["linked_accounts"]:
        c = 1
    if cl["usd_settled"] >= 50:
        c = 2
    d = 0 if not pb["passing"] else 1
    return dict(A=a, B=b, C=c, D=d)


# ------------------------------------------------------------------ scoring + output
def score(sf, cr, cl, pb, bl, shifts, today) -> tuple:
    checks = [
        ("A: named first touches sent", sf["first_today"], f">= {TARGETS['sf_first_touches']}",
         sf["first_today"] >= TARGETS["sf_first_touches"]),
        ("A: follow-ups overdue", len(sf["overdue"]), "0", len(sf["overdue"]) == 0),
        ("B: creator sends", cr["sends_today"], f">= {TARGETS['creator_sends']}", cr["sends_today"] >= TARGETS["creator_sends"]),
        ("C: posts today", cl["posts_today"], f">= {TARGETS['clip_posts_per_account'] * max(1, cl['accounts_posting'])}",
         cl["posts_today"] >= TARGETS["clip_posts_per_account"] * max(1, cl["accounts_posting"])),
        ("C: posts submitted late / unsubmitted", f"{cl['submitted_late']} / {cl['unsubmitted']}", "0 / 0 once linked",
         cl["submitted_late"] == 0),
        ("D: polybot loop alive", "yes" if pb["alive"] else f"no ({pb.get('log_age_min')} min)", "yes", pb["alive"]),
        ("All: shift log entries today", shifts, ">= 1", shifts >= 1),
        ("All: blockers file touched today", bl["updated"].isoformat() if bl["updated"] else "?", today.isoformat(),
         bl["updated"] == today),
    ]
    # the streak counts what the SYSTEM controls; sends depend on the queue and on Alex's clicks
    system_ok = all(ok for name, _, _, ok in checks if name in (
        "A: follow-ups overdue", "C: posts submitted late / unsubmitted", "D: polybot loop alive",
        "All: shift log entries today"))
    return checks, system_ok


def history() -> list:
    txt = _read(PROGRESS_CSV)
    return list(csv.DictReader(io.StringIO(txt))) if txt else []


def write_csv(row: dict):
    hist = [r for r in history() if r.get("date") != row["date"]]
    hist.append(row)
    hist.sort(key=lambda r: r["date"])
    fields = list(row.keys())
    for r in hist:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(PROGRESS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in hist:
            w.writerow({k: r.get(k, "") for k in fields})
    return hist


def streak(hist: list) -> int:
    n = 0
    for r in reversed(hist):
        if _c(r.get("system_ok")) == "1":
            n += 1
        else:
            break
    return n


def main(argv) -> int:
    today = date.today()
    rows = tracker_rows()
    sends = sends_from_log()
    sf = splitframe_metrics(rows, sends, today)
    cr = creator_metrics(rows, sends, today)
    cl = clipping_metrics(today)
    pb = polybot_metrics()
    bl = blockers()
    sh = shifts_today(today)
    money = cash(today)
    st = stages(sf, cr, cl, pb, money)
    checks, system_ok = score(sf, cr, cl, pb, bl, sh, today)

    row = dict(date=today.isoformat(), cash_mtd=money["mtd"], cash_total=money["total"],
               sf_stage=st["A"], sf_first_today=sf["first_today"], sf_fu_today=sf["fu_today"], sf_sent_total=sf["sent_total"],
               sf_named_sent=sf["named_sent"], sf_replies=sf["replies"], sf_calls=sf["calls"], sf_closes=sf["closes"],
               sf_overdue=len(sf["overdue"]), cr_stage=st["B"], cr_sends_today=cr["sends_today"], cr_sent_total=cr["sent_total"],
               cr_replies=cr["replies"], cl_stage=st["C"], cl_posts_today=cl["posts_today"], cl_views=cl["views_total"],
               cl_usd_approved=round(cl["usd_approved"], 2), cl_usd_settled=round(cl["usd_settled"], 2),
               cl_unsubmitted=cl["unsubmitted"], pb_stage=st["D"], pb_alive=int(pb["alive"]),
               pb_gates=" ".join(f"{k}:{v.split(' ')[0]}" for k, v in pb["gates"].items()),
               blockers=bl["dated"] + len(bl["numbered"]), shifts=sh, system_ok=int(system_ok))
    prev = [r for r in history() if r.get("date") != today.isoformat()]
    prev_gates = prev[-1].get("pb_gates", "") if prev else ""
    hist = write_csv(row)
    stk = streak(hist)
    feed = activity(sends, rows, today, prev_gates, pb["gates"])

    def mark(ok):
        return "✓" if ok else "✗"

    L = []
    L.append(f"# PROGRESS — {today.isoformat()}")
    L.append(f"_Rebuilt {datetime.now().strftime('%H:%M')} by `scripts/money_progress.py` from the ledgers. "
             f"Targets and gates: `ROADMAP — money lanes (2026-09-24).md`. Cash comes only from `Revenue.csv`._")
    L.append("")
    L.append(f"**Cash this month: ${money['mtd']:,.0f}** · ever: ${money['total']:,.0f} · "
             f"system streak: **{stk} day(s)** (follow-ups on time, submissions in window, polybot alive, shift logged)")
    L.append("")
    L.append("| Lane | Stage | Next gate |")
    L.append("|---|---|---|")
    L.append(f"| A Splitframe | S{st['A']} | {'first human reply' if st['A']==0 else 'first $650 in the bank' if st['A']==1 else '3 paying clients' if st['A']==2 else '6 retainers'} |")
    L.append(f"| B Creators | S{st['B']} | {'first reply' if st['B']==0 else 'first $400 paid' if st['B']==1 else '5 retainers'} |")
    L.append(f"| C Clipping | S{st['C']} | {'Whop link + first accepted submission' if st['C']==0 else 'first payout >= $50' if st['C']==1 else '$300/month'} |")
    L.append(f"| D Polybot | S{st['D']} | {'bucket_sum 30 sets (' + pb['gates'].get('bucket_sum','?') + ')' if st['D']==0 else 'a live week in the black'} |")
    L.append("")
    L.append("## Today against target")
    L.append("| Check | Today | Target | |")
    L.append("|---|---|---|---|")
    for name, val, tgt, ok in checks:
        L.append(f"| {name} | {val} | {tgt} | {mark(ok)} |")
    L.append("")
    L.append("## Lane detail")
    L.append(f"- **A Splitframe:** {sf['sent_total']} first touches ever ({sf['named_sent']} to a named person), "
             f"{sf['replies']} replies, {sf['calls']} calls, {sf['closes']} closes. Today {sf['first_today']} first touches + "
             f"{sf['fu_today']} follow-ups; {sf['sends_7d']} sends in 7 days; {sf['queue_qualified']} qualified brands unsent.")
    if sf["overdue"]:
        L.append("  - overdue follow-ups: " + "; ".join(f"{b} {k} {d}" for b, k, d in sf["overdue"][:10]))
    if sf["unknown"]:
        L.append(f"  - {len(sf['unknown'])} follow-up date(s) fall before the send log began — unknown, not counted: "
                 + "; ".join(f"{b} {k} {d}" for b, k, d in sf["unknown"][:6]))
    L.append(f"- **B Creators:** {cr['prospects']} prospects, {cr['sent_total']} contacted, {cr['replies']} replies, "
             f"{cr['bounced']} bounced, {cr['closes']} closed. Today {cr['sends_today']} sends.")
    L.append(f"- **C Clipping:** {cl['posts_total']} posts ever, {cl['posts_today']} today across {cl['accounts_posting']} account(s); "
             f"{cl['views_total']:,} views; submitted in window {cl['submitted_in_window']}, late {cl['submitted_late']}, "
             f"unsubmitted {cl['unsubmitted']}; approved ${cl['usd_approved']:.2f}, settled ${cl['usd_settled']:.2f}."
             + (f" ({cl['error']})" if cl["error"] else ""))
    for c in cl.get("campaigns", []):
        L.append(f"  - {c['campaign']}: {c['submitted']} submitted on {c['board'] or '?'}, "
                 f"{c['views_since_submit']:,} views since submission, est ${c['usd_est']:.2f} at ${c['rate']:.2f}/1k"
                 + (f"; blocked: {c['blocked_unlinked']} post(s) / {c['blocked_views']:,} views from accounts not "
                    f"linked on {c['board']}" if c["blocked_unlinked"] else ""))
    gates = ", ".join(f"{k} {v}" for k, v in pb["gates"].items()) or pb.get("error") or "no report"
    L.append(f"- **D Polybot:** loop {'alive' if pb['alive'] else 'NOT alive'}; gates: {gates}."
             + (f" PASSING: {', '.join(pb['passing'])}" if pb["passing"] else ""))
    etas = [f"{k}: {v.split('->')[-1].strip()}" for k, v in pb.get("etas", {}).items() if "->" in v]
    if etas:
        L.append("  - days to gate: " + "; ".join(etas))
    if pb.get("accrual"):
        L.append("  - " + pb["accrual"])
    L.append("")
    L.append(f"## Blocked on Alex ({bl['dated'] + len(bl['numbered'])} items in NEEDS_ALEX)")
    for n in bl["numbered"][:8]:
        L.append(f"- {n}")
    L.append("")
    L.append(f"_Shift log entries today: {sh}. History: `progress.csv`._")
    md = "\n".join(L) + "\n"
    with open(PROGRESS_MD, "w", encoding="utf-8") as f:
        f.write(md)
    line = (f"Money {today.isoformat()}: cash ${money['mtd']:,.0f} mtd · A S{st['A']} {sf['first_today']} sent/"
            f"{len(sf['overdue'])} overdue/{sf['replies']} replies · B S{st['B']} {cr['sends_today']} sent/{cr['replies']} replies · "
            f"C S{st['C']} {cl['posts_today']} posts/{cl['unsubmitted']} unsubmitted · D S{st['D']} "
            f"{pb['gates'].get('bucket_sum','?').split(' ')[0]} · streak {stk}")
    with open(PROGRESS_LINE, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    gate_text = {
        "A": ["first human reply", "first $650 in the bank", "3 paying clients", "6 retainers", "the number"],
        "B": ["first reply", "first $400 paid", "5 retainers", "10 retainers", "the number"],
        "C": ["Whop link + first accepted submission", "first payout >= $50", "$300/month", "$1k/month", "the number"],
        "D": ["bucket_sum 30 sets", "a live week in the black", "second module live", "$1-2k bankroll from profit", "the number"],
    }
    payload = dict(
        date=today.isoformat(), generated_at=int(time.time()), line=line,
        cash=dict(mtd=money["mtd"], total=money["total"]), streak=stk,
        lanes=[dict(id=k, name=n, stage=st[k], next_gate=gate_text[k][min(st[k], 4)]) for k, n in
               (("A", "Splitframe"), ("B", "Creators"), ("C", "Clipping"), ("D", "Polybot"))],
        checks=[dict(name=n, value=str(v), target=t, ok=bool(ok)) for n, v, t, ok in checks],
        detail=dict(
            A=dict(sent_total=sf["sent_total"], named_sent=sf["named_sent"], replies=sf["replies"], calls=sf["calls"],
                   closes=sf["closes"], first_today=sf["first_today"], fu_today=sf["fu_today"], sends_7d=sf["sends_7d"],
                   overdue=[f"{b} {k[:9]} {d}" for b, k, d in sf["overdue"]], queue_qualified=sf["queue_qualified"]),
            B=dict(prospects=cr["prospects"], sent_total=cr["sent_total"], sends_today=cr["sends_today"],
                   replies=cr["replies"], bounced=cr["bounced"], closes=cr["closes"]),
            C=dict(posts_total=cl["posts_total"], posts_today=cl["posts_today"], views=cl["views_total"],
                   submitted_in_window=cl["submitted_in_window"], submitted_late=cl["submitted_late"],
                   unsubmitted=cl["unsubmitted"], usd_approved=round(cl["usd_approved"], 2),
                   usd_settled=round(cl["usd_settled"], 2)),
            D=dict(alive=pb["alive"], gates=pb["gates"], passing=pb["passing"], etas=pb.get("etas", {}), accrual=pb.get("accrual", "")),
        ),
        blockers=dict(count=bl["dated"] + len(bl["numbered"]), items=bl["numbered"][:8]),
        activity=feed,
        history=[dict(date=r.get("date"), cash_mtd=r.get("cash_mtd"), sf_first=r.get("sf_first_today"),
                      sf_fu=r.get("sf_fu_today"), sf_replies=r.get("sf_replies"), cr_sends=r.get("cr_sends_today"),
                      cl_posts=r.get("cl_posts_today"), cl_views=r.get("cl_views"), system_ok=r.get("system_ok"))
                 for r in hist[-14:]],
    )
    import json
    with open(PROGRESS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    if "--print" in argv:
        print(md)
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
