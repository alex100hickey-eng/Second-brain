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
