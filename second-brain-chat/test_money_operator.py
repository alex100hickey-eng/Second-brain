"""money_operator — the server-side driver. No network: Supabase and intake are faked."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import money_operator as mo  # noqa: E402

TZ = ZoneInfo("America/New_York")


class FakeIntake:
    def __init__(self, states=None):
        self.states = dict(states or {})

    def _load_state(self, key):
        return dict(self.states.get(key, {}))

    def _save_state(self, st):
        self.states[st["key"]] = dict(st)


class _Q:
    def __init__(self, sb):
        self.sb, self.kind, self.n = sb, None, 999

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.kind = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self.n = n
        return self

    def insert(self, payload):
        self.sb.rows.append({"id": len(self.sb.rows) + 1, **payload})
        return self

    def execute(self):
        rows = [r for r in reversed(self.sb.rows) if r.get("agent_name") == self.kind][: self.n]
        return type("R", (), {"data": rows})()


class FakeSB:
    def __init__(self):
        self.rows = []

    def table(self, name):
        return _Q(self)


class FakeProactive:
    def __init__(self):
        self.sent = []

    def send_nudge(self, key, title, body, **kw):
        self.sent.append((key, title, body))


def setup(states=None, proactive=None):
    sb, it = FakeSB(), FakeIntake(states)
    mo.init(sb, it, None, proactive)
    return sb, it


def T(h, m=0, day=16):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _pinned_queue_target(monkeypatch):
    """The stock target now follows the live send cadence (money_operator.queue_target), which
    made every ladder test depend on the real tracker's send count. These tests are about which
    task comes next, so pin it."""
    real = mo.queue_target
    monkeypatch.setattr(mo, "_real_queue_target", real, raising=False)
    monkeypatch.setattr(mo, "queue_target", lambda: 10)


def snap(**kw):
    base = {
        "splitframe": {"ok": True, "pending": 10, "draftable_in_band": 0, "hunter_targets_in_band": 0,
                       "hunter_left": 0, "candidates_unread": 0},
        "clip": {"staged": 0, "age_days": 4, "cap": 1, "posted_today": 0, "last_post_ts": None},
        "poly": {"ready_to_promote": []},
        "facts": {}, "done": {}, "idle_reasons": [],
    }
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


# ------------------------------------------------------------------ the hard gates, pinned

def test_rules_file_pins_the_hard_gates():
    text = open(mo.RULES_PATH, encoding="utf-8").read().lower()
    for phrase in ("never send", "splitframe_queue.py add", "never set a polybot module to `live`",
                   "never `git push`", "never create accounts", "never enter credentials",
                   "money_task.py done"):
        assert phrase in text, f"rules file lost the line: {phrase}"


def test_module_has_no_send_or_trade_path():
    src = open(os.path.join(HERE, "money_operator.py")).read()
    for forbidden in ("GMAIL_SEND", "send_email", "smtplib", "approve_send", "arm_auto_send",
                      "place_order", "promote("):
        assert forbidden not in src, forbidden


# ------------------------------------------------------------------ the ladder

def test_topping_up_the_queue_comes_first():
    s = snap(splitframe={"pending": 6, "draftable_in_band": 3}, clip={"staged": 50})
    t = mo.next_task(s, T(20), {})
    assert t["kind"] == "sf_topup" and "+4" in t["title"]
    # a full queue falls through
    s = snap(splitframe={"pending": 10, "draftable_in_band": 3})
    assert mo.next_task(s, T(20), {})["kind"] != "sf_topup"
    # nothing draftable falls through with a reason
    s = snap(splitframe={"pending": 2, "draftable_in_band": 0})
    mo.next_task(s, T(20), {})
    assert any("nothing in band" in r for r in s["idle_reasons"])


def test_founder_addresses_on_hand_are_applied_without_waiting_for_hunter():
    """2026-09-24: Hunter's quota was dry, so the Hunter shift (the only place re-addressing
    lived) never ran, and two published founders sat behind desk drafts going stale."""
    pair = {"brand": "High Mesa Chile Co.", "desk": "info@highmesachile.co",
            "founder": "brock@highmesachile.co", "contact": "Brock Giles", "stale": False}
    s = snap(splitframe={"readdress": [pair], "hunter_left": 0, "pending": 6, "draftable_in_band": 3})
    t = mo.next_task(s, T(12), {})
    assert t["kind"] == "sf_readdress", "before topping up: these drafts are already written"
    assert "info@highmesachile.co -> brock@highmesachile.co" in t["brief"] and "greet Brock" in t["brief"]
    s = snap(splitframe={"named_to_apply": 2, "hunter_left": 0})
    assert mo.next_task(s, T(12), {})["kind"] == "sf_readdress", "unapplied addresses alone are enough"


def test_readdress_respects_quiet_hours_and_its_daily_cap():
    s = snap(splitframe={"named_to_apply": 1})
    mo.next_task(s, T(3), {})
    assert any("sf_readdress" in r for r in s["idle_reasons"])
    s = snap(splitframe={"named_to_apply": 1})
    t = mo.next_task(s, T(12), {"sf_readdress": mo.per_kind_daily(T(12))["sf_readdress"]})
    assert (t or {}).get("kind") != "sf_readdress"


def test_nothing_on_hand_means_no_readdress_task():
    s = snap(splitframe={"readdress": [], "named_to_apply": 0})
    assert (mo.next_task(s, T(12), {}) or {}).get("kind") != "sf_readdress"


def test_the_readdress_brief_spends_nothing_and_flags_stale_drafts():
    b = mo.brief_sf_readdress({"readdress": [{"brand": "BadkneesTs", "desk": "hello@badkneests.com",
                                              "founder": "jim@badkneests.com", "contact": "Jim", "stale": True}]})
    assert "named --write" in b and "no --verify" in b and "revise --to" in b and "--new-to" in b
    assert "re-read the ads first" in b and "Hunter credit" in b


def test_posting_only_in_the_window_under_the_cap_and_three_hours_apart():
    s = snap(clip={"staged": 50, "cap": 1, "posted_today": 0})
    assert mo.next_task(s, T(20), {})["kind"] == "clip_post"
    s = snap(clip={"staged": 50, "cap": 1, "posted_today": 1})
    assert (mo.next_task(s, T(20), {}) or {}).get("kind") != "clip_post"
    s = snap(clip={"staged": 50})
    mo.next_task(s, T(14), {})
    assert any("posting window" in r for r in s["idle_reasons"])
    recent = T(19).timestamp()
    s = snap(clip={"staged": 50, "last_post_ts": recent})
    mo.next_task(s, T(20), {})
    assert any("under 3 h" in r for r in s["idle_reasons"])
    s = snap(clip={"staged": 0})
    mo.next_task(s, T(20), {})
    assert any("nothing staged" in r for r in s["idle_reasons"])


def test_hunter_inside_quota_then_sourcing_when_the_tracker_is_mined_out():
    s = snap(splitframe={"draftable_in_band": 0, "hunter_left": 10, "hunter_targets_in_band": 3})
    assert mo.next_task(s, T(12), {})["kind"] == "sf_hunter"
    s = snap(splitframe={"draftable_in_band": 0, "hunter_left": 0, "hunter_targets_in_band": 3})
    assert mo.next_task(s, T(12), {})["kind"] == "sf_source"
    s = snap(splitframe={"draftable_in_band": 0, "hunter_left": 10, "hunter_targets_in_band": 0})
    assert mo.next_task(s, T(12), {})["kind"] == "sf_source"
    # hunter is once a day
    s = snap(splitframe={"draftable_in_band": 0, "hunter_left": 10, "hunter_targets_in_band": 3},
             done={"sf_hunter": "2026-09-16"})
    assert mo.next_task(s, T(12), {})["kind"] == "sf_source"


def test_daily_kinds_run_once_and_whop_needs_a_login():
    s = snap(splitframe={"draftable_in_band": 2})           # queue full, so no top-up
    assert mo.next_task(s, T(12), {})["kind"] == "poly_review"
    s = snap(splitframe={"draftable_in_band": 2}, done={"poly_review": "2026-09-16"})
    assert mo.next_task(s, T(12), {})["kind"] == "creator_list"
    s = snap(splitframe={"draftable_in_band": 2},
             done={"poly_review": "2026-09-16", "creator_list": "2026-09-16"})
    assert mo.next_task(s, T(12), {}) is None
    assert any("whop" in r for r in s["idle_reasons"])
    s = snap(splitframe={"draftable_in_band": 2}, facts={"whop_logged_in": True},
             done={"poly_review": "2026-09-16", "creator_list": "2026-09-16"})
    assert mo.next_task(s, T(12), {})["kind"] == "whop_board"


def test_quiet_hours_and_per_kind_daily_caps():
    s = snap(splitframe={"pending": 2, "draftable_in_band": 3})
    assert mo.next_task(s, T(3), {}) is None
    assert any("quiet hours" in r for r in s["idle_reasons"])
    s = snap(splitframe={"pending": 2, "draftable_in_band": 3})
    # read the cap from the constant: hardcoding it meant tuning the governor turned this red
    # (2026-09-17, when sf_topup went 3 -> 8) instead of testing the behaviour it names
    t = mo.next_task(s, T(12), {"sf_topup": mo.PER_KIND_DAILY["sf_topup"]})
    assert t["kind"] != "sf_topup" and any("daily cap" in r for r in s["idle_reasons"])


def test_post_cap_and_hunter_cycle():
    assert [mo.post_cap(d) for d in (0, 1, 6, 7, 20, 21)] == [0, 1, 1, 2, 2, 5]
    from datetime import date
    assert mo.hunter_cycle_start(date(2026, 9, 16)) == date(2026, 8, 24)
    assert mo.hunter_cycle_start(date(2026, 9, 24)) == date(2026, 9, 24)
    rows = [{"email_checked": "2026-09-10"}, {"email_checked": "2026-09-15"}, {"email_checked": "2026-08-01"}, {}]
    assert mo.hunter_used(rows, date(2026, 8, 24)) == 2


# ------------------------------------------------------------------ governor + tick

def test_governor_caps_runs_and_enforces_the_gap():
    now = T(12)
    st = {"runs": [(now - timedelta(minutes=5)).isoformat()]}
    assert "gap" in mo.governor_blocks(st, now)
    st = {"runs": [(now - timedelta(minutes=30)).isoformat()]}
    assert mo.governor_blocks(st, now) is None
    cap = mo.max_runs_per_day(now)
    st = {"runs": [(now - timedelta(hours=h)).isoformat() for h in range(1, cap + 1)]}
    assert "usage cap" in mo.governor_blocks(st, now)


def test_the_burst_window_closes_itself():
    """The governor was opened on 2026-09-17 to spend weekly credit that would otherwise expire.
    It has to shut on its own: an opened governor that outlives the credit starves the operator
    every following day, and 'someone remembers to revert it' is not a mechanism."""
    from datetime import timezone as _tz
    during = datetime(2026, 9, 17, 18, 0, tzinfo=_tz.utc)
    after = datetime(2026, 9, 19, 12, 0, tzinfo=_tz.utc)
    assert mo.max_runs_per_day(during) == mo.BURST_RUNS
    assert mo.min_gap_min(during) == mo.BURST_GAP
    assert mo.per_kind_daily(during)["sf_source"] == 8
    assert mo.max_runs_per_day(after) == mo.NORMAL_RUNS == 18
    assert mo.min_gap_min(after) == mo.NORMAL_GAP == 12
    assert mo.per_kind_daily(after)["sf_source"] == 5
    # The burst still has to be the wider setting, or "burst" means nothing.
    assert mo.BURST_RUNS > mo.NORMAL_RUNS and mo.BURST_GAP < mo.NORMAL_GAP


def test_tick_files_waits_settles_and_keeps_going(monkeypatch):
    pro = FakeProactive()
    sb, it = setup(proactive=pro)
    # the real snapshot derives "posted today" from state; the fake does the same
    monkeypatch.setattr(mo, "snapshot", lambda st, now: snap(
        clip={"staged": 50, "posted_today": (st.get("posts") or {}).get("2026-09-16", 0)}))
    r1 = mo.tick(T(20))
    assert "filed" in r1 and r1["task"]["kind"] == "clip_post"
    assert any(r["agent_name"] == mo.TASK_KIND for r in sb.rows)
    # still in flight → waits, files nothing more
    r2 = mo.tick(T(20, 10))
    assert r2 == {"waiting": r1["filed"]}
    # the worker reports done with facts
    mo.mark(r1["filed"], "done", "posted v47",
            {"posted_now": 1, "last_post_ts": T(20, 5).timestamp(), "asks": ["Log in to Vyro once"]})
    r3 = mo.tick(T(20, 45))
    st = it.states[mo.STATE_KEY]
    assert st["posts"]["2026-09-16"] == 1 and st["done"]["clip_post"] == "2026-09-16"
    assert st["facts"]["last_post_ts"] == T(20, 5).timestamp()
    assert st["asks"] == ["Log in to Vyro once"]
    assert pro.sent and "only you can do" in pro.sent[0][1]
    # "keep going": the settle tick files the next rung at once — the post cap is reached and
    # the default snapshot's tracker is mined out, so sourcing goes out — and that is in flight
    assert r3.get("filed") and r3["task"]["kind"] == "sf_source"
    assert st["in_flight"]["kind"] == "sf_source"
    assert st["history"][-1]["status"] == "done"


def test_a_task_nobody_picks_up_is_retired_and_alex_is_told_once(monkeypatch):
    pro = FakeProactive()
    sb, it = setup(proactive=pro)
    monkeypatch.setattr(mo, "snapshot", lambda st, now: snap(splitframe={"pending": 2, "draftable_in_band": 3}))
    r1 = mo.tick(T(12))
    assert "filed" in r1
    r2 = mo.tick(T(14, 30))                       # 150 min later, never picked up
    st = it.states[mo.STATE_KEY]
    assert mo.latest_update(r1["filed"])["status"] == "failed"
    assert st["history"][-1]["note"] == "never picked up"
    assert any(k == "money-operator-pickup" for k, _, _ in pro.sent)
    # it files the next task right away (gap satisfied) rather than waiting for another tick
    assert "filed" in r2
    sent_before = len(pro.sent)
    mo.tick(T(17, 30))
    assert len([k for k, _, _ in pro.sent if k == "money-operator-pickup"]) == 1
    assert sent_before <= len(pro.sent)


def test_a_worker_that_started_and_never_reported_times_out(monkeypatch):
    sb, it = setup()
    monkeypatch.setattr(mo, "snapshot", lambda st, now: snap(splitframe={"pending": 2, "draftable_in_band": 3}))
    r1 = mo.tick(T(12))
    mo.mark(r1["filed"], "in_progress", "worker started")
    assert mo.tick(T(12, 40)) == {"waiting": r1["filed"]}
    # updated_at is written in UTC by mark(); 80 min after the start it is retired
    monkeypatch.setattr(mo, "_now_iso", lambda: (T(12, 1)).isoformat())
    sb.rows[-1]["output_text"] = json.dumps({**json.loads(sb.rows[-1]["output_text"]),
                                             "updated_at": T(12, 1).isoformat()})
    # 96 min in, the worker is STILL WORKING and must not be written off. On 2026-09-17 the real
    # sf_source worker picked up at 09:50 reported done at 11:26 with six brands in the tracker,
    # and the old 75-min cap had already recorded it as "worker timed out" at 11:05.
    mo.tick(T(13, 37))
    assert mo.latest_update(r1["filed"])["status"] == "in_progress"
    # past the cap it is retired, so a genuinely hung worker still frees the slot
    mo.tick(T(14, 10))
    assert mo.latest_update(r1["filed"])["status"] == "failed"


def test_file_task_never_opens_two_of_the_same_kind():
    sb, it = setup()
    a = mo.file_task({"kind": "poly_review", "lane": "polybot", "title": "x", "brief": "y"}, T(12))
    b = mo.file_task({"kind": "poly_review", "lane": "polybot", "title": "x", "brief": "y"}, T(13))
    assert a == b and len([r for r in sb.rows if r["agent_name"] == mo.TASK_KIND]) == 1
    assert [t["slug"] for t in mo.pending_tasks()] == [a]
    mo.mark(a, "done")
    assert mo.pending_tasks() == []


def test_two_failures_retire_a_kind_for_the_day_and_pause_stops_everything(monkeypatch):
    sb, it = setup()
    st = mo.load_state()
    inf = {"slug": "s1", "kind": "sf_source", "filed_at": T(12).isoformat()}
    mo.apply_update(st, inf, {"status": "failed", "note": "boom"}, T(12, 30))
    assert st["done"].get("sf_source") is None
    mo.apply_update(st, inf, {"status": "failed", "note": "boom"}, T(13))
    assert st["done"]["sf_source"] == "2026-09-16"
    mo.save_state(st)
    assert "paused" in mo.handle_tool_call("money_operator", {"action": "pause"})
    assert mo.tick(T(14)) == {"idle": "paused"}
    mo.handle_tool_call("money_operator", {"action": "resume"})
    assert "running" in mo.status_text()


def test_briefs_name_the_guarded_commands_and_numbers():
    b = mo.brief_sf_topup({"pending": 6}, 4)
    assert "splitframe_queue.py add" in b and "6 of the 10" in b
    b = mo.brief_clip_post({"age_days": 4, "cap": 1, "posted_today": 0, "last_post_ts": None, "staged": 187})
    assert "cap 1 post" in b and "Higgsfield" in b and "17:00 and 22:30" in b
    assert "never push" in mo.brief_poly_review({"ready_to_promote": []})
    assert "go-live" in mo.brief_poly_review({"ready_to_promote": ["weather_lock"]})
    assert "splitframe_queue.py source" in mo.brief_sf_source({})


# ------------------------------------------------------------------ the headless page reader

def _load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_adlib_reader_boils_a_page_down_to_ads():
    ar = _load(os.path.join(mo.ROOT, "scripts", "adlib_read.py"))
    html = ("<html><body><script>var x=1;</script><div>~60 results</div>"
            "<div>Library ID: 111</div><div>Started running on Aug 4, 2026</div><div>Platforms</div>"
            "<div>Dagne Dover</div><div>Sponsored</div><div>Stay organized from workout to work.</div>"
            "<div>Library ID: 222</div><div>Started running on Aug 6, 2026</div>"
            "<div>Jessica Lo with Dagne Dover</div><div>Sponsored</div><div>code JESS20</div></body></html>")
    text = ar.html_to_text(html)
    assert "var x" not in text
    parsed = ar.parse_ads(text)
    assert parsed["count"] == 60 and parsed["ad_blocks_seen"] == 2
    assert parsed["ads"][0]["id"] == "111" and parsed["ads"][0]["started"] == "Aug 4, 2026"
    assert "Stay organized" in parsed["ads"][0]["text"] and "Platforms" not in parsed["ads"][0]["text"]
    assert ar.parse_keyword(text) == ["Dagne Dover", "Jessica Lo"]


def test_watcher_prompt_carries_rules_brief_and_report_commands():
    cw = _load(os.path.join(mo.ROOT, "scripts", "capability_watcher.py"))
    task = {"slug": "sf_topup-20260916-2010", "lane": "splitframe", "title": "Top up", "brief": "Queue 3."}
    p = cw.money_prompt(task, "RULES HERE")
    assert p.startswith("RULES HERE") and "Queue 3." in p
    assert "money_task.py done --slug sf_topup-20260916-2010" in p
    assert "money_task.py blocked --slug sf_topup-20260916-2010" in p
    assert "never ask a question" in p


def test_slugs_are_unique_within_a_minute():
    """The server files the next task in the same tick the last one reports, so two tasks of one
    kind land in the same minute routinely. A repeated slug makes pending_tasks read the FIRST
    task's terminal update and drop the second while its worker is still running — after which
    the "never two of the same kind open at once" guard cannot see it either."""
    from datetime import datetime
    mo.pending_tasks = lambda: []
    filed = []
    mo.supabase = type("S", (), {
        "table": lambda self, _n: type("T", (), {
            "insert": lambda self, row: type("E", (), {
                "execute": lambda self: filed.append(row)})()})()})()
    t0 = datetime(2026, 9, 17, 17, 15, 18)
    t1 = datetime(2026, 9, 17, 17, 15, 49)
    a = mo.file_task({"kind": "creator_draft", "lane": "creator", "title": "x", "brief": "y"}, t0)
    b = mo.file_task({"kind": "creator_draft", "lane": "creator", "title": "x", "brief": "y"}, t1)
    assert a != b, f"same slug for two tasks 31s apart: {a}"
    assert a.startswith("creator_draft-20260917-1715")


# ---------------------------------------------------------------------------
# Supply has to be able to feed demand. A send cap with no drafts behind it is
# theatre: at 3 topup runs x 5 drafts the ceiling was 15 a day against a cap
# that now ramps to 20, so the queue would have emptied in two days and the
# raised cap would have released nothing. This is the invariant to keep if
# anyone moves either number again.
# ---------------------------------------------------------------------------

def test_drafting_capacity_can_feed_the_send_cap():
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "sfd", os.path.expanduser("~/second-brain/scripts/splitframe_daily.py"))
    sfd = _ilu.module_from_spec(spec)
    spec.loader.exec_module(sfd)
    max_cap = max(cap for _thr, cap in sfd.RAMP)
    capacity = mo.PER_KIND_NORMAL["sf_topup"] * mo.DRAFTS_PER_RUN
    assert capacity >= max_cap, (
        f"{capacity} drafts/day cannot feed a {max_cap}/day send cap")


def test_the_run_budget_can_fund_the_splitframe_lane():
    """The per-kind caps bind harder than MAX_RUNS — if they sum past it, the lane that
    matters silently loses the tail of its day to whatever ran first."""
    sf_runs = sum(mo.PER_KIND_NORMAL[k] for k in ("sf_topup", "sf_source", "sf_hunter"))
    assert mo.NORMAL_RUNS >= sf_runs, "splitframe alone cannot fit in the daily run budget"


def test_the_stock_target_follows_the_send_cadence(monkeypatch):
    fake = type("Q", (), {"current_per_day": staticmethod(lambda: (15, "x")),
                          "current_runway_target": staticmethod(lambda n: 2 * n)})
    monkeypatch.setattr(mo, "_sq", lambda: fake)
    assert mo._real_queue_target() == 30


def test_stock_target_falls_back_when_the_cadence_is_unreadable(monkeypatch):
    def boom():
        raise RuntimeError("no queue module")
    monkeypatch.setattr(mo, "_sq", boom)
    assert mo._real_queue_target() == mo.QUEUE_TARGET


def test_only_the_revenue_lane_was_raised():
    """Clip posting is bounded by the account-safety rule that exists because 13 clips in
    5 hours killed @wildest_moments. Having budget is not a reason to push it."""
    assert mo.PER_KIND_NORMAL["clip_post"] == 3
    assert mo.PER_KIND_NORMAL["sf_hunter"] == 1      # bounded by the real Hunter quota
    assert mo.PER_KIND_NORMAL["poly_review"] == 1


# ---------------------------------------------------------------------------
# The creator lane's share of the shared queue. It was a flat count sized
# against a 10-deep queue; doubling the queue target silently halved the lane's
# share, quieting the lane that has never sent an email in favour of the one
# with 23 sends and 0 replies. Same bug as the hard-coded QUEUE_TARGET.
# ---------------------------------------------------------------------------

def test_creator_reserve_holds_its_share_as_the_queue_grows(monkeypatch):
    for target, expected in ((10, 2), (20, 4), (40, 8)):
        monkeypatch.setattr(mo, "queue_target", lambda t=target: t)
        assert mo.creator_reserve() == expected, target


def test_creator_reserve_never_drops_below_the_floor(monkeypatch):
    """Below 2 the lane cannot test its offer at all."""
    monkeypatch.setattr(mo, "queue_target", lambda: 5)
    assert mo.creator_reserve() == mo.CREATOR_RESERVE == 2


def test_creator_draft_is_gated_by_the_share_not_the_constant():
    import inspect
    src = inspect.getsource(mo.next_task)
    assert "creator_reserve()" in src
    assert "< CREATOR_RESERVE" not in src, "still reading the flat constant"
