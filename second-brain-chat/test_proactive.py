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

    def __call__(self, topic, title, body, priority, tags):
        if self.fail:
            raise RuntimeError("ntfy unreachable")
        self.sent.append({"topic": topic, "title": title, "body": body,
                          "priority": priority, "tags": tags})


def _reset(tracker=None, sender=None, topic="test-topic"):
    sb = FakeSB()
    intake.init(claude_client=FakeClaude(), supabase_client=sb,
                tool_dispatcher=lambda s, a: "{}", tracker=tracker or FakeTracker())
    proactive.init(claude_client=FakeClaude(), supabase_client=sb,
                   tool_dispatcher=lambda s, a: "{}",
                   tracker=tracker or FakeTracker(), intake_module=intake)
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
        def __call__(self, topic, title, body, priority, tags):
            import urllib.request
            req = urllib.request.Request(
                f"{proactive.NTFY_SERVER}/{topic}", data=body.encode(),
                headers={"Title": proactive._header_safe(title), "Priority": priority,
                        "Tags": proactive._header_safe(tags), "Click": proactive.DEEP_LINK})
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
    st["concerns"]["due:task:9"]["last"] = (
        datetime.now() - timedelta(hours=7)).isoformat()
    proactive._save_sent_state(st)
    out = proactive.send_nudge("due:task:9", "T", "B", renudge_hours=6)
    check("deadline escalation within the day", out.startswith("Nudge sent"))
    st = proactive._sent_state()
    st["concerns"]["due:task:9"]["last"] = (
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
    total, passed = len(_results), sum(_results)
    print("\n" + "=" * 48)
    print(f"{passed}/{total} checks passed")
    print("=" * 48)
    sys.exit(0 if passed == total else 1)
