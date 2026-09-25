"""business_monitor — the layer above liveness. No network."""
import business_monitor as bm


class FakeIntake:
    def __init__(self, states): self.states = states
    def _load_state(self, key): return dict(self.states.get(key, {}))


class FakeOutbox:
    def __init__(self, items): self.items = items
    def open_items(self, limit=60): return list(self.items)


def _iso(hours_ago):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


def test_an_empty_funnel_is_a_problem_even_though_nothing_is_broken():
    """Every process green and no drafted first touches left means nothing goes out tomorrow.
    That is the failure this monitor exists for — healthy, and earning nothing."""
    bm.init(FakeIntake({"splitframe:firsttouch_queue": {"queue": [{"released": "x"}]}}), FakeOutbox([]))
    s = bm.splitframe_status()
    assert any("funnel is EMPTY" in p for p in s["problems"])


def test_a_full_funnel_is_quiet():
    bm.init(FakeIntake({"splitframe:firsttouch_queue": {"queue": [{"to": "a@b.com"}]}}), FakeOutbox([]))
    assert bm.splitframe_status()["problems"] == []


def test_an_approved_email_stuck_on_a_sleeping_mac_is_raised():
    bm.init(FakeIntake({"splitframe:firsttouch_queue": {"queue": [{"to": "a@b.com"}]}}),
            FakeOutbox([{"kind": "email_draft", "title": "Send the reply to x@y.com",
                         "send_approved": _iso(9)}]))
    assert any("stuck" in p for p in bm.splitframe_status()["problems"])
    bm.init(FakeIntake({"splitframe:firsttouch_queue": {"queue": [{"to": "a@b.com"}]}}),
            FakeOutbox([{"kind": "email_draft", "title": "Send the reply to x@y.com",
                         "send_approved": _iso(1)}]))
    assert bm.splitframe_status()["problems"] == []


def test_dead_reach_is_reported_as_a_problem_not_a_statistic():
    bm.init(FakeIntake({"business:clipbot": {
        "at": _iso(0.2), "stats": {"posts": 13, "views": 247, "expected_usd": 0.49},
        "risky_campaigns": ["The Shards E6-7"]}}), None)
    probs = bm.clipbot_status()["problems"]
    assert any("reach is dead" in p for p in probs)
    assert any("forbid transformation" in p for p in probs)


def test_a_module_that_passed_its_gate_is_surfaced_as_alexs_decision():
    bm.init(FakeIntake({"business:polybot": {
        "at": _iso(0.1), "modes": {"weather_lock": "paper"}, "ready_to_promote": ["weather_lock"],
        "live_orders": 0, "bankroll_usd": 200}}), None)
    assert any("READY TO GO LIVE" in p for p in bm.polybot_status()["problems"])


def test_a_lane_that_stops_reporting_is_noticed():
    bm.init(FakeIntake({"business:polybot": {"at": _iso(30), "modes": {}, "bankroll_usd": 200}}), None)
    assert any("not reported" in p for p in bm.polybot_status()["problems"])
    bm.init(FakeIntake({}), None)
    assert any("never published" in p for p in bm.polybot_status()["problems"])


# --- problem_id: the once-a-day guard for "<lane> needs you" pushes -------------------------

def test_a_changing_count_or_age_is_the_same_problem():
    a = "an approved email to x@y.com has been stuck 6h — the Mac has not been awake to send it"
    b = "an approved email to x@y.com has been stuck 7h — the Mac has not been awake to send it"
    assert bm.problem_id(a) == bm.problem_id(b)
    assert (bm.problem_id("reach is dead: 2054 views across 30 posts.")
            == bm.problem_id("reach is dead: 1864 views across 29 posts."))


def test_different_problems_stay_different():
    assert (bm.problem_id("an approved email to x@y.com has been stuck 6h")
            != bm.problem_id("an approved email to z@y.com has been stuck 6h"))


def test_the_problem_id_survives_a_restart():
    """hash(str) is salted per process; six identical pushes on 2026-09-24 were six deploys."""
    import os
    import subprocess
    import sys
    code = "import business_monitor as b; print(b.problem_id('reach is dead: 2054 views across 30 posts.'))"
    here = os.path.dirname(os.path.abspath(__file__))
    ids = {subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True, text=True,
                          env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
           for seed in ("1", "2", "3")}
    assert len(ids) == 1 and ids != {""}


def test_app_keys_the_daily_guard_on_problem_id():
    import os
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
               encoding="utf-8").read()
    assert "business_monitor.problem_id(text)" in src
    assert "abs(hash(text))" not in src
