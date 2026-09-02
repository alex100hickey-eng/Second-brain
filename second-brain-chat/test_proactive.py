"""
Tests for the proactive engine (proactive.py).

Run directly:  python3 test_proactive.py
No network, no real Supabase/Claude/ntfy — fake store + a stubbed sender.

Covers:
  1. config — defaults, set/get roundtrip, persistence in the shared state store
  2. respect rules — quiet hours (incl. midnight wrap), daily cap, never-twice
     key, disabled switch, missing topic → nothing can ever send
  3. delivery — success logged as sent, sender failure logged as failed (no crash)
  4. gather — intake items due soon and task titles with "(due …)" both surface
  5. awareness pass — deadline nudge (high priority when close), batched intake
     nudge, morning-brief one-shot, everything suppressed under quiet hours
"""

import json
import os
import sys
from datetime import datetime, timedelta

import intake
import proactive
from test_intake import FakeSB, FakeClaude, FakeTracker

PASS, FAIL = "PASS    ", "**FAIL**"
_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"{PASS if cond else FAIL} {label}")


class SpySender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, topic, title, body, priority, tags, click="", actions=None):
        if self.fail:
            raise RuntimeError("ntfy unreachable")
        self.sent.append({"topic": topic, "title": title, "body": body,
                          "priority": priority, "tags": tags,
                          "click": click, "actions": actions or []})


def _reset(tracker=None, sender=None, topic="test-topic"):
    sb = FakeSB()
    intake.init(claude_client=FakeClaude(), supabase_client=sb,
                tool_dispatcher=lambda s, a: "{}", tracker=tracker or FakeTracker())
    proactive.init(claude_client=FakeClaude(), supabase_client=sb,
                   tool_dispatcher=lambda s, a: "{}",
                   tracker=tracker or FakeTracker(), intake_module=intake)
    # daily_orders._task_orders reaches for the REAL task_tracker.db via
    # get_tracker(), which bypasses the tracker injected above — so an "empty
    # day" here would silently inherit whatever is on Alex's actual to-do list.
    import task_tracker as _tt
    _tt.get_tracker = lambda *a, **k: type(
        "_T", (), {"top_by_priority": lambda self, limit=20: []})()
    if topic is None:
        os.environ.pop("NTFY_TOPIC", None)
    else:
        os.environ["NTFY_TOPIC"] = topic
    spy = sender or SpySender()
    proactive._sender = spy
    return sb, spy


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _quiet_config_now(active: bool):
    """Set quiet hours so 'now' is inside (active) or outside (inactive) them."""
    now = datetime.now()
    if active:
        proactive.set_config(quiet_start=_hhmm(now - timedelta(hours=1)),
                             quiet_end=_hhmm(now + timedelta(hours=1)))
    else:
        proactive.set_config(quiet_start=_hhmm(now + timedelta(hours=2)),
                             quiet_end=_hhmm(now + timedelta(hours=3)))


# ============================================================
def test_config():
    print("\n=== 1. config get/set ===")
    _reset()
    cfg = proactive.get_config()
    check("defaults load", cfg["max_per_day"] == 8 and cfg["enabled"] is True)
    proactive.set_config(max_per_day=3, quiet_start="23:00")
    cfg = proactive.get_config()
    check("set_config persists changes", cfg["max_per_day"] == 3
          and cfg["quiet_start"] == "23:00")
    proactive.set_config(bogus_key="x")
    check("unknown keys ignored", "bogus_key" not in proactive.get_config())


def test_respect_rules():
    print("\n=== 2. respect rules ===")
    sb, spy = _reset()
    _quiet_config_now(active=False)
    out = proactive.send_nudge("k1", "T", "B")
    check("clear rules → sent", out.startswith("Nudge sent") and len(spy.sent) == 1)
    out = proactive.send_nudge("k1", "T", "B")
    check("same key never nudges twice", "already nudged" in out and len(spy.sent) == 1)

    _quiet_config_now(active=True)
    out = proactive.send_nudge("k2", "T", "B")
    check("quiet hours block", "quiet hours" in out and len(spy.sent) == 1)
    out = proactive.send_nudge("k2b", "T", "B", force=True)
    check("force bypasses quiet hours (live testing)", out.startswith("Nudge sent"))

    sb, spy = _reset()
    _quiet_config_now(active=False)
    proactive.set_config(max_per_day=2)
    proactive.send_nudge("a", "T", "B")
    proactive.send_nudge("b", "T", "B")
    out = proactive.send_nudge("c", "T", "B")
    check("daily cap enforced", "daily cap" in out and len(spy.sent) == 2)

    sb, spy = _reset()
    proactive.set_config(enabled=False)
    out = proactive.send_nudge("k", "T", "B")
    check("disabled switch blocks everything", "disabled" in out and spy.sent == [])

    sb, spy = _reset(topic=None)
    _quiet_config_now(active=False)
    out = proactive.send_nudge("k", "T", "B")
    check("no NTFY_TOPIC → nothing can send", "no NTFY_TOPIC" in out and spy.sent == [])

    # midnight wrap: 22:00–08:00 blocks 23:30 and 07:00, allows 12:00
    cfg = {"quiet_start": "22:00", "quiet_end": "08:00"}
    real_now = proactive._now
    for hhmm, expect in (("23:30", True), ("07:00", True), ("12:00", False)):
        proactive._now = lambda h=hhmm: datetime.strptime(f"2026-07-22 {h}", "%Y-%m-%d %H:%M")
        check(f"midnight-wrap quiet hours: {hhmm} → {'in' if expect else 'out'}",
              proactive._in_quiet_hours(cfg) is expect)
    proactive._now = real_now


def test_emoji_headers():
    print("\n=== 2b. emoji-safe HTTP headers (real bug: latin-1 header crash) ===")
    check("emoji title round-trips through the header-safe encoding",
          proactive._header_safe("👋 Test").encode("latin-1").decode("utf-8") == "👋 Test")
    real_sender = proactive._post_ntfy

    class RealPostButNoNetwork:
        """Exercises the real header-building path without hitting the network."""
        def __call__(self, topic, title, body, priority, tags, click="", actions=None):
            import urllib.request
            headers = {"Title": proactive._header_safe(title), "Priority": priority,
                       "Tags": proactive._header_safe(tags),
                       "Click": click or proactive.DEEP_LINK}
            built = proactive._actions_header(actions)
            if built:
                headers["Actions"] = proactive._header_safe(built)
            req = urllib.request.Request(
                f"{proactive.NTFY_SERVER}/{topic}", data=body.encode(), headers=headers)
            req.header_items()  # would raise UnicodeEncodeError pre-fix

    sb, _ = _reset(sender=RealPostButNoNetwork())
    _quiet_config_now(active=False)
    out = proactive.send_nudge("emoji1", "👋 CLARVIS test nudge", "⏰ due now", tags="alarm_clock")
    check("emoji title/body never crashes the sender", out.startswith("Nudge sent"))


def test_delivery_logging():
    print("\n=== 3. delivery logging ===")
    sb, spy = _reset()
    _quiet_config_now(active=False)
    proactive.send_nudge("ok", "Works", "B")
    rows = proactive._nudge_rows()
    check("success logged as sent", any(n["status"] == "sent" for n in rows))
    sb, spy = _reset(sender=SpySender(fail=True))
    _quiet_config_now(active=False)
    out = proactive.send_nudge("boom", "T", "B")
    check("sender failure → 'failed' logged, no crash",
          "delivery failed" in out
          and any(n["status"] == "failed" for n in proactive._nudge_rows()))


def test_gather():
    print("\n=== 4. gather ===")
    tracker = FakeTracker()
    sb, spy = _reset(tracker=tracker)
    soon = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")
    intake.record_raw("imessage", "g1", "Mom", "", "dentist",
                      items=[{"type": "event", "text": "Dentist appt", "due": soon}])
    tracker.top_by_priority = lambda limit=10: [
        {"id": 7, "title": f"Send registration (due {soon})", "status": "idea"}]
    picture = proactive._gather()
    check("intake item due soon surfaces", any(d["ref"].startswith("intake:")
                                               for d in picture["due_soon"]))
    check("task '(due …)' title surfaces", any(d["ref"] == "task:7"
                                               for d in picture["due_soon"]))
    check("untriaged count present", picture["new_intake"] == 1)


def test_awareness_pass():
    print("\n=== 5. awareness pass ===")
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    close = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    intake.record_raw("imessage", "g1", "Mom", "", "dentist",
                      items=[{"type": "event", "text": "Dentist appt", "due": close}])
    out = proactive.run_awareness_pass()
    check("due-soon deadline nudges", any("Dentist" in s["title"] for s in spy.sent))
    check("≤3h away → high priority", any(s["priority"] == "high" for s in spy.sent))
    out2 = proactive.run_awareness_pass()
    check("second pass suppressed by the key", "0 sent" in out2)

    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    for i in range(3):
        intake.record_raw("imessage", f"n{i}", "x", "",
                          f"thing {i}", items=[{"type": "info", "text": f"thing {i}",
                                                "due": None}])
    proactive.run_awareness_pass()
    check("intake pile-up → ONE batched nudge",
          sum(1 for s in spy.sent if "triage" in s["title"]) == 1)

    # Empty day → NO brief. "0 open, 0 due" is noise, not a notification.
    sb, spy = _reset(tracker=tracker)
    proactive.set_config(morning_brief=_hhmm(datetime.now()), evening_review="")
    _quiet_config_now(active=False)
    proactive.run_awareness_pass()
    check("empty day → morning brief SKIPPED", spy.sent == [])

    # Day with substance → brief fires, NAMES the content, and is one-shot.
    busy = FakeTracker()
    busy.top_by_priority = lambda limit=10: [
        {"id": 3, "title": "Ship the taste-pass pack", "status": "in_progress"}]
    sb, spy = _reset(tracker=busy)
    proactive.set_config(morning_brief=_hhmm(datetime.now()), evening_review="")
    _quiet_config_now(active=False)
    proactive.run_awareness_pass()
    briefs = [s for s in spy.sent if "Today:" in s["title"]]
    check("busy day → morning brief fires", len(briefs) == 1)
    check("brief names the actual work",
          briefs and "taste-pass" in briefs[0]["body"])
    n = len(spy.sent)
    proactive.run_awareness_pass()
    check("brief is one-shot per day", len(spy.sent) == n)

    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=True)
    intake.record_raw("imessage", "g9", "Mom", "", "dentist",
                      items=[{"type": "event", "text": "Dentist appt", "due": close}])
    out = proactive.run_awareness_pass()
    check("quiet hours suppress the whole pass", spy.sent == []
          and "suppressed" in out)


def test_concern_caps():
    print("\n=== 6. per-concern caps (once or twice per thing) ===")
    sb, spy = _reset()
    _quiet_config_now(active=False)
    proactive.set_config(max_per_day=8, max_per_concern=2,
                         renudge_after_hours=20, concern_window_days=7)

    # Date-suffixed keys collapse to one concern across days.
    check("date suffix stripped",
          proactive._base_key("august:overdue:stripe:2026-08-05") == "august:overdue:stripe")
    check("half-day suffix stripped",
          proactive._base_key("intake:2026-08-05-AM") == "intake")

    # Send #1 today, send #2 "tomorrow", then NOTHING despite more days passing.
    out1 = proactive.send_nudge("august:step:stripe:2026-08-05", "T", "B")
    check("concern send #1 goes out", out1.startswith("Nudge sent"))
    st = proactive._sent_state()
    st["concerns"]["august:step:stripe"]["last"] = (
        datetime.now() - timedelta(hours=25)).isoformat()
    proactive._save_sent_state(st)
    out2 = proactive.send_nudge("august:step:stripe:2026-08-06", "T", "B")
    check("concern send #2 (next day) goes out", out2.startswith("Nudge sent"))
    st = proactive._sent_state()
    st["concerns"]["august:step:stripe"]["last"] = (
        datetime.now() - timedelta(hours=25)).isoformat()
    proactive._save_sent_state(st)
    out3 = proactive.send_nudge("august:step:stripe:2026-08-07", "T", "B")
    check("concern send #3 MUTED (cap of 2)", "muted" in out3 and len(spy.sent) == 2)

    # After a full quiet window the concern earns a fresh pair.
    st = proactive._sent_state()
    st["concerns"]["august:step:stripe"]["last"] = (
        datetime.now() - timedelta(days=8)).isoformat()
    proactive._save_sent_state(st)
    out4 = proactive.send_nudge("august:step:stripe:2026-08-15", "T", "B")
    check("quiet window resets the concern", out4.startswith("Nudge sent"))

    # renudge_hours override allows a same-day escalation; cap still ends it.
    sb, spy = _reset()
    _quiet_config_now(active=False)
    proactive.send_nudge("due:task:9", "T", "B", renudge_hours=6)
    st = proactive._sent_state()
    st["concerns"][proactive._base_key("due:task:9")]["last"] = (
        datetime.now() - timedelta(hours=7)).isoformat()
    proactive._save_sent_state(st)
    out = proactive.send_nudge("due:task:9", "T", "B", renudge_hours=6)
    check("deadline escalation within the day", out.startswith("Nudge sent"))
    st = proactive._sent_state()
    st["concerns"][proactive._base_key("due:task:9")]["last"] = (
        datetime.now() - timedelta(hours=7)).isoformat()
    proactive._save_sent_state(st)
    out = proactive.send_nudge("due:task:9", "T", "B", renudge_hours=6)
    check("but never a third", "muted" in out and len(spy.sent) == 2)

    # recurring windows are exempt from the lifetime cap but keep their spacing.
    sb, spy = _reset()
    _quiet_config_now(active=False)
    for day in (1, 2, 3):
        proactive.send_nudge(f"morning_brief:2026-08-0{day}", "T", "B", recurring=True)
        st = proactive._sent_state()
        st["concerns"]["morning_brief"]["last"] = (
            datetime.now() - timedelta(hours=25)).isoformat()
        proactive._save_sent_state(st)
    check("recurring window fires daily, no lifetime cap", len(spy.sent) == 3)


def test_pileup_growth_gate():
    print("\n=== 7. intake pile-up growth gate ===")
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    for i in range(4):
        intake.record_raw("imessage", f"p{i}", "x", "", f"thing {i}",
                          items=[{"type": "info", "text": f"thing {i}", "due": None}])
    proactive.run_awareness_pass()
    first = sum(1 for s in spy.sent if "triage" in s["title"])
    check("pile-up fires on first sight", first == 1)
    # Same pile a day later — no growth → silence, even with spacing elapsed.
    st = proactive._sent_state()
    st["concerns"]["intake-pileup"]["last"] = (
        datetime.now() - timedelta(hours=25)).isoformat()
    proactive._save_sent_state(st)
    proactive.run_awareness_pass()
    check("static pile does NOT re-nudge",
          sum(1 for s in spy.sent if "triage" in s["title"]) == 1)
    # Pile grows by 5 → one more nudge.
    for i in range(4, 9):
        intake.record_raw("imessage", f"p{i}", "x", "", f"thing {i}",
                          items=[{"type": "info", "text": f"thing {i}", "due": None}])
    proactive.run_awareness_pass()
    check("grown pile re-nudges once",
          sum(1 for s in spy.sent if "triage" in s["title"]) == 2)


def test_one_concern_and_away_days():
    print("\n=== due+missed are ONE concern; away days silence block pings; "
          "Canvas deadlines never go MISSED ===")
    check("due: and missed: collapse to one concern",
          proactive._base_key("due:intake:5") == "item:intake:5"
          and proactive._base_key("missed:intake:5:2026-09-01") == "item:intake:5")
    check("other keys are untouched", proactive._base_key("outbox:12") == "outbox:12")
    # Legacy ledger entries fold into the merged key with their counts summed.
    sb0, _ = _reset()
    st = intake._load_state(proactive.SENT_KEY)
    st["concerns"] = {"due:intake:5": {"n": 2, "last": "2026-09-01T10:00:00"},
                      "missed:intake:5": {"n": 1, "last": "2026-09-02T00:00:00"}}
    intake._save_state(st)
    folded = proactive._sent_state()["concerns"]
    check("legacy due:/missed: counts fold into one item: concern",
          folded.get("item:intake:5", {}).get("n") == 3 and "due:intake:5" not in folded
          and folded["item:intake:5"]["last"].startswith("2026-09-02"))
    now = datetime.now()

    def blk(h, m, title, dur_min=30):
        s = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return {"title": title, "start": s, "end": s + timedelta(minutes=dur_min)}

    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    real_blocks, real_away = proactive._today_blocks, proactive._away_on
    try:
        sb, spy = _reset(tracker=tracker)
        proactive.set_config(morning_brief="", evening_review="")
        _quiet_config_now(active=False)
        proactive._today_blocks = lambda n: [blk(n.hour, n.minute, "Class review + study 1:50–2:40")]
        proactive._away_on = lambda d: True
        proactive.run_awareness_pass()
        check("an away day silences the session kickoff",
              not any("Session start" in s["title"] for s in spy.sent))
        sb, spy = _reset(tracker=tracker)
        proactive.set_config(morning_brief="", evening_review="")
        _quiet_config_now(active=False)
        proactive._away_on = lambda d: False
        proactive.run_awareness_pass()
        check("… and a normal day still pings", any("Session start" in s["title"] for s in spy.sent))
    finally:
        proactive._today_blocks, proactive._away_on = real_blocks, real_away
    check("_away_on reads the calendar words", bool(proactive._AWAY_RE.search("AWAY — Boston"))
          and bool(proactive._AWAY_RE.search("Flying back 7:15 AM"))
          and not proactive._AWAY_RE.search("MATH Test 1 (in class)"))
    sb, spy = _reset(tracker=tracker)
    past = (proactive._now() - timedelta(hours=5)).isoformat()
    intake._insert_event({"source": "gmail_school", "source_ref": "cv-1",
                          "sender": "\"Foundations of Accounting I\" <notifications@instructure.com>",
                          "ts": past, "preview": "…", "status": "new",
                          "items": [{"type": "deadline", "text": "Homework 2 due by 11:59 PM", "due": past}]})
    intake._insert_event({"source": "gmail", "source_ref": "own-1",
                          "sender": "Alex Hickey <alexhickey@splitframestudio.com>",
                          "ts": past, "preview": "…", "status": "new",
                          "items": [{"type": "commitment", "text": "get the first outreach batch moving", "due": past}]})
    pic = proactive._gather()
    check("a Canvas deadline never lands in MISSED",
          not any("Homework 2" in d["what"] for d in pic["overdue"]))
    check("his own sent mail never lands in due-soon or MISSED",
          not any("outreach batch" in d["what"] for d in pic["overdue"] + pic["due_soon"]))


def test_wake_and_sessions():
    print("\n=== 8. wake-time brief + session kickoffs ===")
    now = datetime.now()

    def blk(h, m, title, dur_min=30):
        s = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return {"title": title, "start": s, "end": s + timedelta(minutes=dur_min)}

    # _wake_target resolution ladder
    wake_blocks = [blk(6, 30, "Wake 6:30 · morning routine → 7:00"),
                   blk(9, 0, "MATH 120 · 9:20–10:10 · Olin 305")]
    check("wake cell names the moment",
          proactive._wake_target(now, wake_blocks) == (6, 30, True))
    no_wake = [blk(3, 0, "Sleep 10:00 → wake"), blk(10, 0, "ACCT 100 · PBL 201")]
    check("no wake cell → first non-Sleep block",
          proactive._wake_target(now, no_wake) == (10, 0, True))
    check("blank day → 08:15 fallback, quiet hours kept",
          proactive._wake_target(now, []) == (8, 15, False))

    # Wake-mode brief fires INSIDE quiet hours (that is the whole point) …
    busy = FakeTracker()
    busy.top_by_priority = lambda limit=10: [
        {"id": 3, "title": "Ship the taste-pass pack", "status": "in_progress"}]
    sb, spy = _reset(tracker=busy)
    proactive.set_config(morning_brief="wake", evening_review="")
    _quiet_config_now(active=True)
    real_blocks = proactive._today_blocks
    proactive._today_blocks = lambda n: [blk(n.hour, n.minute, "Wake · morning routine")]
    try:
        proactive.run_awareness_pass()
        check("wake-mode brief pierces quiet hours",
              any("Today:" in s["title"] for s in spy.sent))

        # … but the 08:15 fallback does NOT (grid-less day, quiet stays law).
        sb, spy = _reset(tracker=busy)
        proactive.set_config(morning_brief="wake", evening_review="",
                             quiet_start="08:00", quiet_end="09:00")
        proactive._today_blocks = lambda n: []
        real_now = proactive._now
        proactive._now = lambda: datetime.now().replace(hour=8, minute=20)
        try:
            proactive.run_awareness_pass()
            check("grid-less fallback still respects quiet hours", spy.sent == [])
        finally:
            proactive._now = real_now

        # Session kickoff: study block starting now pings once, exactly once.
        tracker = FakeTracker()
        tracker.top_by_priority = lambda limit=10: []
        sb, spy = _reset(tracker=tracker)
        proactive.set_config(morning_brief="", evening_review="")
        _quiet_config_now(active=False)
        proactive._today_blocks = lambda n: [
            blk(n.hour, n.minute, "Class review + study 1:50–2:40"),
            blk(n.hour, n.minute, "Gym · 3:00–7:00"),               # not a session
            blk((n.hour + 3) % 24, n.minute, "Study / work later"),  # outside window
        ]
        proactive.run_awareness_pass()
        pings = [s for s in spy.sent if "Session start" in s["title"]]
        check("study block starting now → one kickoff ping", len(pings) == 1)
        check("gym / future blocks stay silent", len(spy.sent) == 1)
        proactive.run_awareness_pass()
        check("kickoff is one-shot per block",
              sum(1 for s in spy.sent if "Session start" in s["title"]) == 1)

        # Kill switch.
        sb, spy = _reset(tracker=tracker)
        proactive.set_config(morning_brief="", evening_review="", session_nudges=False)
        _quiet_config_now(active=False)
        proactive._today_blocks = lambda n: [blk(n.hour, n.minute, "Study / work")]
        proactive.run_awareness_pass()
        check("session_nudges=False disables kickoffs", spy.sent == [])
    finally:
        proactive._today_blocks = real_blocks


def test_actionable_nudges():
    """The 2026-08-23 ask: a notification has to CARRY the action, not describe it.

    Alex: "it would be helpful if CLARVIS could notify me things like hey, these
    emails need to go out right now, click this button to review and send them."
    So the checks are about what a nudge SHIPS — a tap target, buttons, and steps —
    not just about whether it fired."""
    print("\n=== 9. nudges carry the action ===")
    os.environ["ACTION_LINK_SECRET"] = "test-secret-for-links"
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    close = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    intake.record_raw("imessage", "a1", "Mom", "", "dentist",
                      items=[{"type": "event", "text": "Dentist appt", "due": close}])
    proactive.run_awareness_pass()
    due = [s for s in spy.sent if "Dentist" in s["title"]]
    check("a deadline nudge fires", len(due) == 1)
    check("tapping it lands on the item's own page, not the generic dashboard",
          due and "/do/" in due[0]["click"])
    labels = [a["label"] for a in (due[0]["actions"] if due else [])]
    check("it ships a Done button that closes it from the shade", "Done" in labels)
    check("…and a Snooze button, so 'not now' is a real answer",
          any("Snooze" in l for l in labels))
    check("no more buttons than ntfy will render",
          len(labels) <= proactive.MAX_ACTIONS)


def test_missed_items():
    """"When there's something I need to be doing that I am not." A thing whose
    moment has passed gets its OWN nudge, worded as missed — never re-dressed as a
    fresh deadline, which is how a real deadline stops being believed."""
    print("\n=== 10. missed items ===")
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    past = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
    intake.record_raw("imessage", "m1", "Coach Staley", "", "lift",
                      items=[{"type": "task", "text": "Confirm lift time", "due": past}])
    picture = proactive._gather()
    check("a 6h-late item is classified missed, not due-soon",
          any(d["ref"].startswith("intake:") for d in picture["overdue"])
          and not picture["due_soon"])
    proactive.run_awareness_pass()
    missed = [s for s in spy.sent if s["title"].startswith("⚠️ Missed")]
    check("it produces a missed nudge", len(missed) == 1)
    check("the body says how late it is, in his words",
          missed and "6h ago" in missed[0]["body"])
    check("it offers an honest way out, not just 'done'",
          missed and any("Snooze" in a["label"] for a in missed[0]["actions"]))

    # Ancient items are archaeology, not reminders.
    sb, spy = _reset(tracker=tracker)
    ancient = (datetime.now() - timedelta(hours=proactive.OVERDUE_HOURS + 5)
               ).strftime("%Y-%m-%dT%H:%M")
    intake.record_raw("imessage", "m2", "x", "", "old",
                      items=[{"type": "task", "text": "Ancient thing", "due": ancient}])
    check("past the horizon it stops surfacing entirely",
          proactive._gather()["overdue"] == [])

    # A backlog is one fact about the day, not eight buzzes.
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    for i in range(5):
        intake.record_raw("imessage", f"m{i}x", "x", "", f"late {i}",
                          items=[{"type": "task", "text": f"Late thing {i}",
                                  "due": past}])
    proactive.run_awareness_pass()
    check("missed nudges are capped per pass",
          sum(1 for s in spy.sent if s["title"].startswith("⚠️ Missed"))
          <= proactive.OVERDUE_MAX_PER_PASS)

    # An attendance event whose moment passed is NOT missed — CLARVIS can't see
    # whether Alex showed up, and he doesn't report in. It just drops.
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    intake.record_raw("imessage", "ev1", "Coach Staley", "", "team mtg",
                      items=[{"type": "event", "text": "Team meeting, HoF room",
                              "due": past}])
    picture = proactive._gather()
    check("a passed event never lands in overdue",
          picture["overdue"] == [] and picture["due_soon"] == [])
    proactive.run_awareness_pass()
    check("…and produces no missed nudge",
          not any(s["title"].startswith("⚠️ Missed") for s in spy.sent))


def test_waiting_on_alex():
    """The email case, end to end: a draft CLARVIS wrote is a finished thing that
    decays in a folder until his thumb moves. It has to nudge, carry the mailbox
    link, and close in one tap."""
    print("\n=== 11. waiting on his hand ===")
    import outbox
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    outbox.init(sb)
    oid = outbox.add("email_draft", "Send the reply to coach@case.edu",
                     detail="Subject: Re: lift times", steps=["Open drafts", "Send"],
                     link="https://mail.google.com/mail/u/0/#drafts")
    proactive.run_awareness_pass()
    check("a just-written draft does NOT buzz — he's still in that conversation",
          not any("Ready to send" in s["title"] for s in spy.sent))

    # Age it past its quiet period, through the module's own writer.
    outbox._write(oid, {"created": (datetime.now() - timedelta(hours=4)).isoformat()})
    spy.sent.clear()
    proactive.run_awareness_pass()
    ready = [s for s in spy.sent if "Ready to send" in s["title"]]
    check("once it has aged, it nudges", len(ready) == 1)
    labels = [a["label"] for a in ready[0]["actions"]] if ready else []
    check("the notification carries the button that STARTS the job",
          "Review & send" in labels)
    check("…and the one-tap 'Sent it' that ends it", "Sent it" in labels)
    check("Review & send points at the mailbox, not at CLARVIS",
          ready and any(a["url"].startswith("https://mail.google.com")
                        for a in ready[0]["actions"]))
    check("tapping the notification opens the item's page",
          ready and "/do/" in ready[0]["click"])

    # Closing it is the whole contract: the nudge stops.
    outbox.close(oid, outbox.DONE)
    spy.sent.clear()
    proactive.run_awareness_pass(force=True)
    check("once he says it went out, it stops nudging — forever",
          not any("Ready to send" in s["title"] for s in spy.sent))


def test_approval_needs_him():
    """CLARVIS deciding it needs a human and then waiting silently is the same
    failure as the unsent draft. It has to say so — but Approve is NOT a
    lock-screen button: a tap on a shade is not consent."""
    print("\n=== 12. blocked on a decision ===")
    import json as _json
    tracker = FakeTracker()
    tracker.top_by_priority = lambda limit=10: []
    sb, spy = _reset(tracker=tracker)
    _quiet_config_now(active=False)
    proactive.set_config(morning_brief="", evening_review="")
    sb.table("Agent Outputs").insert({
        "agent_name": "jarvis_pending_action",
        "output_text": _json.dumps({"status": "pending",
                                    "display": "Adopt tool: quiz_builder",
                                    "action": "adopt_tool"})}).execute()
    proactive.run_awareness_pass()
    ask = [s for s in spy.sent if "needs your call" in s["title"]]
    check("a pending decision reaches his phone", len(ask) == 1)
    check("it names what is blocked",
          ask and "quiz_builder" in ask[0]["title"] + ask[0]["body"])
    labels = [a["label"] for a in ask[0]["actions"]] if ask else []
    check("approve is NOT offered as a shade button", "Approve" not in labels)
    check("it opens the page where the action is visible", labels == ["Review it"])


# ============================================================
if __name__ == "__main__":
    test_config()
    test_respect_rules()
    test_emoji_headers()
    test_delivery_logging()
    test_gather()
    test_awareness_pass()
    test_concern_caps()
    test_pileup_growth_gate()
    test_wake_and_sessions()
    test_actionable_nudges()
    test_missed_items()
    test_waiting_on_alex()
    test_approval_needs_him()
    test_one_concern_and_away_days()
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
