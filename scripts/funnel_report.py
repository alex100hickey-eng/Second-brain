#!/usr/bin/env python3
"""Splitframe funnel report: which close, which kind of inbox, and which wave is working.

Replies are 0 as of 2026-09-23, so today's numbers aren't the point. The point is that when a
reply lands, the report already says which close it answered, what kind of inbox it came from
and which wave the brand was sourced in. There is no digging through the tracker to find out.

    python3 scripts/funnel_report.py                  # write Money/Funnel — <date>.md
    python3 scripts/funnel_report.py --stdout         # print it instead of writing it
    python3 scripts/funnel_report.py --send-log PATH  # read sends from somewhere else

READ-ONLY on the tracker. The only file this ever writes is the report itself. It sends nothing,
drafts nothing and stamps nothing. Pinned by test_funnel_report.

Where each number comes from, and what each source can't see:
  tracker   first touches (sent_date), replies (replied), calls (call_date), the close arm
            (close_variant), wave, and the follow-up DUE dates. It has no record of whether a
            follow-up actually went.
  send log  scripts/splitframe_send.log is the Mac sender's own record of every email that
            left, first touches and follow-ups alike. It exists on the Mac only. A follow-up
            sent by hand from Gmail never reaches it.
  drafted   the daily job's follow-up state (address -> touches drafted). This is what the
            server has instead of a send log. Drafted is not the same as sent: the daily cap
            can hold a draft back.
If neither follow-up source can be read, "done" prints as unknown, never as 0. A record that
can't be read must not look like "every follow-up was missed", or like "all caught up".

Mac only for the file. The daily job (splitframe_daily.main) runs on the server, where it logs
summary_line() and nothing else. Two machines writing the same vault path on the same day would
be a git conflict in vault sync.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# Who an address belongs to is decided by the daily job, since it picks follow-up targets with
# it. Reusing it means a reply gets credited to the tier the drafter actually used. A second copy
# of these rules here would be the one that drifts.
_SPEC = importlib.util.spec_from_file_location(
    "splitframe_daily", os.path.join(HERE, "splitframe_daily.py"))
_sfd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_sfd)

LOCAL_TZ = _sfd.LOCAL_TZ
VAULT = _sfd.VAULT
TRACKER = _sfd.TRACKER
# Beside this file, for the same reason as splitframe_daily.LOG: it resolves to the checkout
# that's running, on whichever machine it runs.
SEND_LOG = os.path.join(HERE, "splitframe_send.log")
SENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \S+ item \d+: SENT to (\S+)")

# A reference rate, not a claim about this business. Cold outreach usually replies at 1-5%, and
# the only use here is saying how surprising a run of zeroes is.
REFERENCE_REPLY_RATE = 0.03

TIER_LABELS = {"person": "named contact", "shared": "generic inbox",
               "ticket": "support ticket", "none": "no address"}
ARM_LABELS = {"offer": "offer", "question": "question",
              "pre-split": "question, sent before the A/B"}
LANE_LABELS = {"splitframe": "Splitframe (ad creative)", "creator": "creator clips ($400/mo)"}
FU_COLUMNS = ((2, "followup1_date"), (3, "followup2_date"))    # touch 2 = FU1, touch 3 = FU2


def _s(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def _d(value):
    try:
        return date.fromisoformat(_s(value)[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ reading (never writing)

def tracker_rows(path: str = None) -> list:
    with open(path or TRACKER, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def parse_send_log(text: str) -> dict:
    """address -> sorted list of dates an email to it left the Mac."""
    out = {}
    for line in (text or "").splitlines():
        m = SENT_RE.match(line)
        if not m:
            continue
        when = _d(m.group(1))
        if when:
            out.setdefault(m.group(2).strip().lower(), []).append(when)
    for dates in out.values():
        dates.sort()
    return out


def read_send_log(path: str = None):
    """The parsed log, or None when there is no log to read. None means unknown, and it must
    never be treated as an empty log: that would turn every due follow-up into "overdue"."""
    try:
        with open(path or SEND_LOG, encoding="utf-8", errors="replace") as f:
            return parse_send_log(f.read())
    except OSError:
        return None


# ------------------------------------------------------------------ classifying one row

def lane(row: dict) -> str:
    """Keyed on category the same way the drafter picks its voice (splitframe_daily.voice_for)."""
    return "creator" if _s(row.get("category")).lower() == "creator" else "splitframe"


def arm(row: dict) -> str:
    """The close this row got. A blank means it went out before the A/B started on 2026-09-17,
    and those were all the question close. They stay a separate arm here: they went to named
    founders from the early waves, which is a different list, so they are evidence for neither
    arm. An unrecognised value gets its own row rather than being folded in quietly."""
    return _s(row.get("close_variant")).lower() or "pre-split"


def tier(row: dict) -> str:
    """person / shared / ticket / none: the inbox the drafter writes to for this row."""
    _addr, t = _sfd.target_address(row)
    if t:
        return t
    for col in ("email", "email_generic"):
        e = _s(row.get(col)).lower()
        if "@" in e and _sfd.is_ticket_desk(e):
            return "ticket"
    return "none"


def wave(row: dict) -> str:
    return _s(row.get("wave")) or "none"


def addresses(row: dict) -> set:
    return {_s(row.get(c)).lower() for c in ("email", "email_generic")} - {""}


def followup_sends(row: dict, sends) -> list:
    """Dates follow-ups to this row left the Mac, oldest first. Only sends on or after the FU1
    due date count. A first touch logged a day off its sent_date must not read as FU1, and no
    follow-up can be drafted before it's due."""
    start = _d(row.get("followup1_date"))
    if not start:
        first = _d(row.get("sent_date"))
        start = first + timedelta(days=1) if first else None
    if not start:
        return []
    return sorted(d for a in addresses(row) for d in sends.get(a, []) if d >= start)


def followup_states(row: dict, today: date, sends=None, drafted=None) -> list:
    """[(touch, due_date, state)] for FU1 and FU2.

    States: sent, drafted, overdue, due today, due (unverified), upcoming, stopped (the brand
    replied or was closed out, so it is owed nothing).
    """
    stopped = bool(_s(row.get("replied")) or _s(row.get("outcome")))
    fu_sent = followup_sends(row, sends) if sends is not None else None
    done = None
    if drafted is not None:
        addr, _t = _sfd.target_address(row)
        done = drafted.get(addr, []) if addr else []
    out = []
    for i, (touch, col) in enumerate(FU_COLUMNS):
        due = _d(row.get(col))
        if not due:
            continue
        if fu_sent is not None and len(fu_sent) > i:
            state = "sent"
        elif done is not None and touch in done:
            state = "drafted"
        elif stopped:
            state = "stopped"
        elif due > today:
            state = "upcoming"
        elif fu_sent is None and done is None:
            state = "due (unverified)"
        elif due == today:
            state = "due today"
        else:
            state = "overdue"
        out.append((touch, due, state))
    return out


# ------------------------------------------------------------------ the report

def _slice(rows: list, today: date, sends, drafted) -> dict:
    sent = [r for r in rows if _d(r.get("sent_date"))]
    replied = [r for r in sent if _s(r.get("replied"))]
    calls = [r for r in sent if _s(r.get("call_date"))]
    owed = done = 0
    for r in sent:
        for _touch, due, state in followup_states(r, today, sends, drafted):
            if state in ("sent", "drafted") or (due <= today and state != "stopped"):
                owed += 1
                done += state in ("sent", "drafted")
    last = max((_d(r.get("sent_date")) for r in sent), default=None)
    return {"sent": len(sent), "replied": len(replied), "calls": len(calls),
            "fu_owed": owed, "fu_done": done, "last_first_touch": last}


def _group(rows: list, key, today, sends, drafted, order=(), always=()) -> list:
    """Slices in `order`, then any unexpected keys. Keys in `always` show even at zero sends,
    because "0 sent to support tickets" is information and a missing row is not."""
    buckets = {k: [] for k in always}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    names = [k for k in order if k in buckets] + sorted(k for k in buckets if k not in order)
    return [(k, _slice(buckets[k], today, sends, drafted)) for k in names]


def build(rows: list, today: date, sends=None, drafted=None) -> dict:
    """Everything the report says, as data. Pure: no file reads and no clock."""
    sent_rows = [r for r in rows if _d(r.get("sent_date"))]
    sf = [r for r in sent_rows if lane(r) == "splitframe"]

    fu = {"sent": 0, "drafted": 0, "overdue": [], "due today": [], "due (unverified)": [],
          "upcoming_7d": 0, "upcoming": 0}
    for r in sent_rows:
        for touch, due, state in followup_states(r, today, sends, drafted):
            if state in ("sent", "drafted"):
                fu[state] += 1
            elif state in ("overdue", "due today", "due (unverified)"):
                fu[state].append({"brand": _s(r.get("brand")), "touch": touch, "due": due,
                                  "late": (today - due).days, "lane": lane(r)})
            elif state == "upcoming":
                fu["upcoming"] += 1
                fu["upcoming_7d"] += (due - today).days <= 7
    for k in ("overdue", "due today", "due (unverified)"):
        fu[k].sort(key=lambda x: (-x["late"], x["brand"]))

    responses = []
    for r in sent_rows:
        if not (_s(r.get("replied")) or _s(r.get("call_date")) or _s(r.get("outcome"))):
            continue
        after = None
        replied_on = _d(r.get("replied"))
        if sends is not None and replied_on:
            after = 1 + sum(1 for d in followup_sends(r, sends) if d <= replied_on)
        responses.append({"brand": _s(r.get("brand")), "lane": lane(r), "arm": arm(r),
                          "tier": tier(r), "wave": wave(r), "sent": _d(r.get("sent_date")),
                          "replied": _s(r.get("replied")), "call": _s(r.get("call_date")),
                          "outcome": _s(r.get("outcome")), "after_touch": after})

    # Sends with no tracker row get no follow-up clock at all, because the clock lives in the
    # tracker. This is how creator-lane prospects got exactly one email on 2026-09-19.
    untracked = []
    last_any = None
    if sends is not None:
        known = set().union(*(addresses(r) for r in rows)) if rows else set()
        for addr, dates in sends.items():
            last_any = max(last_any, dates[-1]) if last_any else dates[-1]
            if addr not in known:
                untracked.append({"to": addr, "first": dates[0], "count": len(dates)})
        untracked.sort(key=lambda x: (x["first"], x["to"]))

    return {
        "today": today,
        "fu_source": ("send log" if sends is not None
                      else "drafted state" if drafted is not None else None),
        "splitframe": _slice(sf, today, sends, drafted),
        "by_arm": _group(sf, arm, today, sends, drafted, order=("offer", "question", "pre-split"),
                         always=("offer", "question")),
        "by_tier": _group(sf, tier, today, sends, drafted,
                          order=("person", "shared", "ticket", "none"),
                          always=("person", "shared", "ticket")),
        "by_wave": _group(sf, wave, today, sends, drafted, order=("1", "2", "3", "4", "none")),
        "by_lane": _group(sent_rows, lane, today, sends, drafted,
                          order=("splitframe", "creator")),
        "followups": fu,
        "responses": responses,
        "untracked": untracked,
        "last_first_touch": max((_d(r.get("sent_date")) for r in sent_rows), default=None),
        "last_any_send": last_any,
    }


# ------------------------------------------------------------------ rendering

def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def _ago(when, today: date) -> str:
    if not when:
        return "never"
    days = (today - when).days
    return "today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"


def _fu_cell(s: dict, source) -> str:
    return f"{s['fu_done']}/{s['fu_owed']}" if source else f"?/{s['fu_owed']}"


def _table(title: str, groups: list, labels: dict, source, today: date) -> list:
    out = [f"## {title}", "",
           "| | sent | replies | reply rate | calls | call rate | follow-ups done/due | last first touch |",
           "|---|---:|---:|---:|---:|---:|---:|---|"]
    for key, s in groups:
        last = s["last_first_touch"]
        out.append(f"| {labels.get(key, key)} | {s['sent']} | {s['replied']} | "
                   f"{_pct(s['replied'], s['sent'])} | {s['calls']} | {_pct(s['calls'], s['sent'])} | "
                   f"{_fu_cell(s, source)} | "
                   f"{f'{last} ({_ago(last, today)})' if last else '—'} |")
    out.append("")
    return out


def headline(rep: dict) -> list:
    sf, fu, today = rep["splitframe"], rep["followups"], rep["today"]
    lines = [f"Splitframe lane: **{sf['sent']} first touches, {sf['replied']} "
             f"repl{'y' if sf['replied'] == 1 else 'ies'}, {sf['calls']} "
             f"call{'' if sf['calls'] == 1 else 's'}.**"]
    last = f"Last first touch {rep['last_first_touch'] or 'never'} ({_ago(rep['last_first_touch'], today)})"
    if rep["last_any_send"]:
        last += f"; last email of any kind {rep['last_any_send']} ({_ago(rep['last_any_send'], today)})"
    lines.append(last + ".")
    owed = fu["sent"] + fu["drafted"] + len(fu["overdue"]) + len(fu["due today"]) \
        + len(fu["due (unverified)"])
    if rep["fu_source"] == "send log":
        lines.append(f"Follow-ups: {owed} due so far, {fu['sent']} sent, "
                     f"**{len(fu['overdue'])} overdue**, {len(fu['due today'])} due today.")
    elif rep["fu_source"] == "drafted state":
        lines.append(f"Follow-ups: {owed} due so far, {fu['drafted']} drafted, "
                     f"**{len(fu['overdue'])} never drafted**, {len(fu['due today'])} due today.")
    else:
        lines.append(f"Follow-ups: {owed} due so far. **Whether they went is unknown**: "
                     "no send log or drafted state was readable here.")
    if sf["sent"] and not sf["replied"]:
        chance = (1 - REFERENCE_REPLY_RATE) ** sf["sent"]
        lines.append(f"Zero replies from {sf['sent']} would still happen {chance:.0%} of the time "
                     f"if the true reply rate were {REFERENCE_REPLY_RATE:.0%}. It isn't a "
                     "verdict on any close or tier yet.")
    return lines


def summary_line(rep: dict) -> str:
    """One line for the daily job's output."""
    sf, fu = rep["splitframe"], rep["followups"]
    miss = "never drafted" if rep["fu_source"] == "drafted state" else "overdue"
    fu_part = (f"{len(fu['overdue'])} follow-up(s) {miss}" if rep["fu_source"]
               else "follow-up status unknown")
    arms = ", ".join(f"{k} {s['replied']}/{s['sent']}" for k, s in rep["by_arm"])
    tiers = ", ".join(f"{TIER_LABELS.get(k, k)} {s['replied']}/{s['sent']}"
                      for k, s in rep["by_tier"])
    return (f"funnel: {sf['sent']} sent · {sf['replied']} replied · {sf['calls']} calls · "
            f"{fu_part} · replies/sent by close: {arms} · by inbox: {tiers} · "
            f"last first touch {_ago(rep['last_first_touch'], rep['today'])}")


def render(rep: dict) -> str:
    today, fu, src = rep["today"], rep["followups"], rep["fu_source"]
    out = [f"# Funnel — {today}", "",
           f"*Generated {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} by "
           "`scripts/funnel_report.py`. Read-only on the tracker. Regenerate any time.*", ""]
    out += headline(rep) + [""]

    out += ["## Replies and calls", ""]
    if rep["responses"]:
        out += ["| brand | lane | close | inbox | wave | first touch | replied | after touch | call | outcome |",
                "|---|---|---|---|---|---|---|---:|---|---|"]
        for x in rep["responses"]:
            out.append(f"| {x['brand']} | {x['lane']} | {ARM_LABELS.get(x['arm'], x['arm'])} | "
                       f"{TIER_LABELS.get(x['tier'], x['tier'])} | {x['wave']} | {x['sent']} | "
                       f"{x['replied'] or '—'} | {x['after_touch'] or '—'} | {x['call'] or '—'} | "
                       f"{x['outcome'] or '—'} |")
    else:
        out.append("None yet. When one lands, it shows up here with its close, inbox and wave.")
    out.append("")

    out += _table("By close (Splitframe lane)", rep["by_arm"], ARM_LABELS, src, today)
    out += ["The A/B compares **offer** with **question**, sent side by side from the same list "
            "since 2026-09-17. The pre-A/B sends went to named founders from the early waves, "
            "which is a different list, so they count as evidence for neither arm.", ""]
    out += _table("By inbox (Splitframe lane)", rep["by_tier"], TIER_LABELS, src, today)
    out += ["Named contact = a founder's or staff member's own address. Generic inbox = hello@, "
            "info@ and the like. Support ticket = support@, orders@ and similar, which the "
            "drafter never writes to. The tier is the address the drafter targets for the row "
            "today.", ""]
    out += _table("By wave (Splitframe lane)", rep["by_wave"], {"none": "no wave"}, src, today)
    out += _table("By lane", rep["by_lane"], LANE_LABELS, src, today)

    out += ["## Follow-ups", ""]
    if src == "send log":
        out.append("Done means it's in the Mac send log. A follow-up sent by hand from Gmail "
                   "won't show up there.")
    elif src == "drafted state":
        out.append("Done means drafted (this machine has no send log). The daily cap can "
                   "still be holding a drafted email.")
    else:
        out.append("**No send log and no drafted state could be read, so the due dates below "
                   "can't be checked against anything.**")
    out.append("")
    late_label = {"send log": "Overdue (due, not in the send log)",
                  "drafted state": "Due and never drafted"}.get(src, "Due (unverified)")
    late = fu["overdue"] if src else fu["due (unverified)"]
    out.append(f"**{late_label}: {len(late)}**")
    for x in late:
        out.append(f"- {x['brand']}: FU{x['touch'] - 1} due {x['due']} "
                   f"({x['late']} day{'' if x['late'] == 1 else 's'} late)"
                   + (" · creator lane" if x["lane"] == "creator" else ""))
    if fu["due today"]:
        out += ["", f"Due today: {len(fu['due today'])}"]
        out += [f"- {x['brand']}: FU{x['touch'] - 1}" for x in fu["due today"]]
    out += ["", f"Upcoming: {fu['upcoming']} ({fu['upcoming_7d']} in the next 7 days).", ""]

    out += ["## Sends with no tracker row", ""]
    if rep["fu_source"] != "send log":
        out.append("Unknown. This needs the send log.")
    elif rep["untracked"]:
        out.append("These have **no follow-up clock**, because the clock lives in the tracker. "
                   "Each got one email and nothing will ever follow it up.")
        out += [f"- {x['to']}: first sent {x['first']}, {x['count']} send(s)"
                for x in rep["untracked"]]
    else:
        out.append("None. Every email in the send log belongs to a tracker row.")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ the one write

def report_path(today: date) -> str:
    return os.path.join(VAULT, "Money", f"Funnel — {today.isoformat()}.md")


def write_report(text: str, path: str) -> str:
    """Write the report and nothing else. It refuses any path that isn't a .md file, and above
    all refuses the tracker itself."""
    real = os.path.realpath(path)
    if real == os.path.realpath(TRACKER) or not real.endswith(".md"):
        raise ValueError(f"refusing to write {path}: the report only ever writes a .md file")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stdout", action="store_true", help="print the report, write nothing")
    ap.add_argument("--send-log", default=None, help=f"send log to read (default {SEND_LOG})")
    args = ap.parse_args(argv)

    today = datetime.now(LOCAL_TZ).date()
    try:
        rows = tracker_rows()
    except OSError as exc:
        print(f"funnel: tracker unreadable ({exc}). No report written.", file=sys.stderr)
        return 1
    rep = build(rows, today, sends=read_send_log(args.send_log))
    text = render(rep)
    if args.stdout:
        print(text)
        return 0
    path = write_report(text, report_path(today))
    print(summary_line(rep))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
