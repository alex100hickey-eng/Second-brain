"""Tests for the Splitframe daily operator (scripts/splitframe_daily.py). No network."""
import ast
import csv
import importlib.util
import os
from datetime import date, datetime, timedelta

import pytest

# This checkout's scripts/, not ~/second-brain: a worktree must test its own copy.
DAILY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "splitframe_daily.py")
SPEC = importlib.util.spec_from_file_location("splitframe_daily", DAILY)
sfd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sfd)


@pytest.fixture(autouse=True)
def _front_desks_allowed(monkeypatch):
    """These tests are about the cap, the queue, staleness and conflicts, not about who gets
    written to. The named-person policy (NAMED_ONLY) is pinned in test_named_only.py."""
    monkeypatch.setattr(sfd, "NAMED_ONLY", False)


def _row(**kw):
    base = {"brand": "Acme", "email": "founder@acme.com", "sent_date": "2026-09-01",
            "followup1_date": "2026-09-14", "followup2_date": "2026-09-18",
            "replied": "", "outcome": "", "contact_name": "Dana Reed"}
    base.update(kw)
    return base


TODAY = date(2026, 9, 15)


def test_no_send_capability():
    """The guardrail the whole business rests on: this module drafts, Alex sends. If someone ever
    wires a send call in here, this test is what stops it shipping."""
    src = open(DAILY).read()
    for forbidden in ("GMAIL_SEND_EMAIL", "GMAIL_SEND", "send_email", "smtplib", "SEND_DRAFT"):
        assert forbidden not in src, f"a send path appeared in splitframe_daily: {forbidden}"


def test_due_followup_is_found_once_and_in_order():
    rows = [_row()]
    due = sfd.due_followups(rows, TODAY, {})
    assert [(r["brand"], t) for r, t in due] == [("Acme", 2)]
    # after FU1 is drafted, FU2 is not due yet (Sep 18)
    assert sfd.due_followups(rows, TODAY, {"founder@acme.com": [2]}) == []
    # on Sep 18 it is
    due2 = sfd.due_followups(rows, date(2026, 9, 18), {"founder@acme.com": [2]})
    assert [t for _, t in due2] == [3]
    # and after three touches the brand is left alone for good
    assert sfd.due_followups(rows, date(2026, 12, 1), {"founder@acme.com": [2, 3]}) == []


def test_a_reply_stops_the_sequence():
    """Chasing someone who already answered is the one follow-up mistake that costs the
    relationship and not just the email."""
    assert sfd.due_followups([_row(replied="2026-09-13")], TODAY, {}) == []
    assert sfd.due_followups([_row(outcome="not a fit")], TODAY, {}) == []


def test_unsent_and_future_dates_are_not_followed_up():
    assert sfd.due_followups([_row(sent_date="")], TODAY, {}) == []
    assert sfd.due_followups([_row(followup1_date="2026-09-20", followup2_date="2026-09-25")], TODAY, {}) == []
    assert sfd.due_followups([_row(followup1_date="", followup2_date="")], TODAY, {}) == []


def test_first_touch_queue_skips_support_desks():
    rows = [_row(brand="Real", email="founder@real.com", sent_date=""),
            _row(brand="Desk", email="support@desk.com", sent_date=""),
            _row(brand="Desk2", email="help@desk2.co", sent_date=""),
            _row(brand="Already", email="x@already.com", sent_date="2026-09-01"),
            _row(brand="NoEmail", email="", sent_date="")]
    assert [r["brand"] for r in sfd.waiting_for_first_touch(rows)] == ["Real"]


def test_the_real_tracker_still_has_the_columns_this_reads(tmp_path):
    """The tracker is hand-edited and shared with other sessions; a renamed column would make
    this job silently find nothing to do, which is exactly how the follow-ups went missing."""
    if not os.path.exists(sfd.TRACKER):
        pytest.skip("tracker not on this machine")
    with open(sfd.TRACKER, newline="") as f:
        cols = set(csv.DictReader(f).fieldnames or [])
    for needed in ("brand", "email", "sent_date", "followup1_date", "followup2_date",
                   "replied", "outcome", "contact_name"):
        assert needed in cols, f"tracker lost the {needed!r} column"


def test_fabrication_guard_catches_the_real_failures():
    """The first live run produced all three of these. The whole pitch rests on Alex only claiming
    work he did: an invented crate-life number or a concept he never built is worse than silence."""
    assert sfd.fabrication_risk("Pulled the account again — Father's Day ad's finally down")
    assert sfd.fabrication_risk("I actually built the rescue concept instead of just pitching it")
    assert sfd.fabrication_risk("I ran it against your warranty length: closer to 40 cents a day")
    assert sfd.fabrication_risk("New thing  since   I wrote: ...")        # whitespace-insensitive


def test_fabrication_guard_allows_an_honest_followup():
    honest = ("Two weeks, so this probably slid down the inbox. Fair.\n\n"
              "The thing I keep coming back to from that first note is the split. Nine versions of "
              "one ad means none of them gets enough budget to prove anything.\n\n"
              "Happy to build one concept free if you want to see the alternative.\n\n"
              "Is that lane even a priority right now?")
    assert sfd.fabrication_risk(honest) == []


def test_body_parsing_survives_a_chatty_model():
    """A strict json.loads on the raw reply crashed the first real run: in an unattended job that
    is a missed follow-up with no reason recorded."""
    assert sfd.parse_body('{"body": "two lines"}') == "two lines"
    assert sfd.parse_body('```json\n{"body": "fenced"}\n```') == "fenced"
    assert sfd.parse_body('Here you go:\n{"body": "after preamble"}') == "after preamble"
    assert sfd.parse_body('just the prose, no json at all') == "just the prose, no json at all"
    # An empty body is NOT a body, and it is no longer answered with the raw text either: a
    # reply that was trying to be JSON and failed must go back through the retry loop rather
    # than become the email. Returning the raw string is how `{"body": "` nearly reached a
    # founder (Guzu touch 2, 2026-09-19).
    assert sfd.parse_body('{"body": ""}') == ""
    assert sfd.parse_body('{"body": "cut off mid-sen') == "", "truncated JSON is not prose"


class _FlakyClient:
    """The real client, reproduced: a call that sometimes comes back with no text block at all."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.messages = self

    def create(self, **_kw):
        self.calls += 1
        text = self._replies.pop(0) if self._replies else ""
        blocks = [type("B", (), {"type": "text", "text": text})()] if text else []
        return type("M", (), {"content": blocks})()


GOOD = " ".join(["word"] * 40)


def test_a_flaky_generation_is_retried_not_dropped():
    """One call in a handful returns nothing (verified 2026-09-18 against Gunner Kennels touch 3).
    Unretried that is a prospect's LAST touch dropped in silence: the caller rejects the short
    body, the brand is marked drafted, and nobody ever writes to them again."""
    c = _FlakyClient(["", "", GOOD])
    body = sfd.write_followup(c, "Gunner Kennels", "Emily", 3, "the original email", 17)
    assert len(body.split()) >= sfd.MIN_BODY_WORDS
    assert c.calls == 3


def test_a_good_first_answer_is_not_regenerated():
    """The retry must not cost three calls per follow-up on the normal path."""
    c = _FlakyClient([GOOD, GOOD, GOOD])
    sfd.write_followup(c, "Gunner Kennels", "Emily", 3, "the original email", 17)
    assert c.calls == 1


def test_retrying_gives_up_rather_than_looping():
    """Three empties in a row still returns, and returns something the caller will reject."""
    c = _FlakyClient(["", "", ""])
    body = sfd.write_followup(c, "Gunner Kennels", "Emily", 3, "the original email", 17)
    assert len(body.split()) < sfd.MIN_BODY_WORDS
    assert c.calls == 3


# ---- the send path: where it is allowed to live, and where it must never appear ----

SEND_MARKERS = ("GMAIL_SEND_DRAFT", "GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD",
                "smtplib", "sendmail")
SENDER = os.path.join(os.path.dirname(DAILY), "splitframe_send.py")


def test_the_server_still_cannot_send():
    """The original gate's reason is about this node: it reads untrusted email and runs a model,
    so a model with a send tool is an exfiltration lane. Tapping Send on the /do page only stamps
    an approval — if a send slug ever appears in the server-side chain, that reasoning is broken."""
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ("do_actions.py", "outbox.py", "proactive.py", "action_links.py",
                  "mail_drafts.py", "app.py"):
        path = os.path.join(here, fname)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            # IfExp and friends carry a single node in `body`, not a list of statements.
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and any(m in node.value for m in SEND_MARKERS):
                raise AssertionError(f"{fname}:{node.lineno} gained a send path")


def test_no_model_tool_can_reach_the_sender():
    """splitframe_send.py is reachable by launchd and by Alex, and by nothing the model drives."""
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in os.listdir(here):
        if not fname.endswith(".py") or fname == os.path.basename(__file__):
            continue
        tree = ast.parse(open(os.path.join(here, fname), encoding="utf-8", errors="replace").read())
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any("splitframe_send" in (n or "") for n in names), \
                f"{fname} imports the sender"
            # a dynamic import is the same door with a different handle
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("__import__", "import_module"):
                for arg in node.args:
                    assert not (isinstance(arg, ast.Constant)
                                and "splitframe_send" in str(arg.value)), \
                        f"{fname} dynamically imports the sender"


def test_sender_only_targets_studio_drafts_and_tracked_recipients():
    import importlib.util as iu
    spec = iu.spec_from_file_location("sfs", SENDER)
    sfs = iu.module_from_spec(spec)
    spec.loader.exec_module(sfs)
    assert sfs.parse_ref("gmail:studio:r123") == ("studio", "r123")
    assert sfs.parse_ref("gmail:personal:r9") == ("personal", "r9")
    assert sfs.parse_ref("nonsense") == ("", "")
    assert sfs.parse_ref("") == ("", "")
    assert sfs.recipient_of({"title": "Send the reply to Caelin@Diggs.pet"}) == "caelin@diggs.pet"
    assert sfs.recipient_of({"title": "Something with no address"}) == ""


def test_daily_job_initialises_the_outbox():
    """The chain starts with an outbox row: no row, no nudge, no /do page, no Send button — and
    create_email_draft files that row fail-soft, so forgetting outbox.init() looks like success
    and leaves the draft exactly as invisible as before any of this existed. It did, once."""
    src = open(DAILY).read()
    tree = ast.parse(src)
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
    assert any(isinstance(c.func, ast.Attribute) and c.func.attr == "init"
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "outbox"
               for c in calls), "splitframe_daily.main() never calls outbox.init()"


def test_an_open_outbox_row_blocks_a_second_identical_followup():
    """State said 'not drafted', the outbox said otherwise, and Diggs got two identical
    follow-ups sitting in front of him. The outbox is the authority: it's what Alex actually
    sees. (The server crashed between writing the draft and saving state.)"""
    class FakeOutbox:
        @staticmethod
        def open_items():
            return [{"kind": "email_draft", "title": "Send the reply to Caelin@Diggs.pet"},
                    {"kind": "email_draft", "title": "Send the reply to x@y.com"},
                    {"kind": "task", "title": "Send the reply to ignored@nope.com"},
                    {"kind": "email_draft", "title": "no address in this title"}]
    assert sfd.already_waiting(FakeOutbox) == {"caelin@diggs.pet", "x@y.com"}


def test_already_waiting_survives_a_dead_outbox():
    """Dedup must fail CLOSED-ish: an unreachable outbox returns nothing and the state dict
    still guards, rather than the whole run crashing."""
    class Dead:
        @staticmethod
        def open_items():
            raise RuntimeError("supabase down")
    assert sfd.already_waiting(Dead) == set()


def test_pile_page_hands_out_send_links_for_email_drafts():
    """Alex tapped the notification and nothing happened. With more than one thing waiting the
    nudge points at the PILE page, and every per-item link it built carried only done/snooze/drop
    — so the page he landed on could do everything except the one thing the notification
    promised. An email draft's link must carry `send`; an already-approved one must not."""
    import do_actions
    minted = []

    class FakeAL:
        KIND_OUTBOX, KIND_OUTBOX_ALL = "outbox", "outbox_all"

        @staticmethod
        def url(kind, ref, ops=(), label=""):
            minted.append((ref, tuple(ops)))
            return f"https://x/do/{ref}"

    class FakeOutbox:
        @staticmethod
        def open_items():
            return [{"id": 1, "kind": "email_draft", "title": "Send the reply to a@b.com"},
                    {"id": 2, "kind": "email_draft", "title": "Send the reply to c@d.com",
                     "send_approved": "2026-09-15T12:00:00"},
                    {"id": 3, "kind": "task", "title": "Pay the invoice"}]

        @staticmethod
        def summary_line(it):
            return it["title"] + " — 2h"

    old_al, old_ob = do_actions.al, do_actions.outbox_mod
    do_actions.al, do_actions.outbox_mod = FakeAL, FakeOutbox
    try:
        view = {}
        do_actions._resolve_outbox_all(view)
    finally:
        do_actions.al, do_actions.outbox_mod = old_al, old_ob

    ops = dict(minted)
    assert "send" in ops["1"], "an unsent email draft must offer send"
    assert "send" not in ops["2"], "an already-approved draft must not offer send again"
    assert "send" not in ops["3"], "a non-email item must never offer send"


def test_pile_send_button_approves_every_unsent_draft():
    """Alex read the individual ones, then pressed the pile's own Send button and got
    "Couldn't do that: invalid literal for int() with base 10: ''" — the pile page has no ref,
    and it was being routed into the single-item handler. Five emails silently didn't go."""
    import do_actions
    approved = []

    class FakeOutbox:
        DONE, DROPPED, OPEN = "done", "dropped", "open"

        @staticmethod
        def open_items():
            return [{"id": 1, "kind": "email_draft"},
                    {"id": 2, "kind": "email_draft", "send_approved": "2026-09-15T12:00:00"},
                    {"id": 3, "kind": "task"},
                    {"id": 4, "kind": "email_draft"}]

        @staticmethod
        def approve_send(item_id, source="notification"):
            approved.append(item_id)
            return {"id": item_id}

    old = do_actions.outbox_mod
    do_actions.outbox_mod = FakeOutbox
    try:
        res = do_actions._do_outbox_all("send")
        assert res["ok"] and approved == [1, 4], f"approved {approved}"
        assert "2 emails" in res["message"]
        # an op that only makes sense on one item must not silently do nothing useful
        assert do_actions._do_outbox_all("done")["ok"] is False
        # pressing it twice must not double-approve
        approved.clear()
        FakeOutbox.open_items = staticmethod(
            lambda: [{"id": 1, "kind": "email_draft", "send_approved": "x"}])
        again = do_actions._do_outbox_all("send")
        assert again["ok"] and approved == [] and "all approved already" in again["message"]
    finally:
        do_actions.outbox_mod = old


# ---- the first-touch release queue: the three ways it used to lose or fake work ----

class _FakeShared:
    """Stands in for the intake module's Supabase-backed state."""
    def __init__(self, queue):
        self.state = {"key": sfd.QUEUE_KEY, "queue": queue}

    def _load_state(self, key):
        return self.state

    def _save_state(self, st):
        self.state = st


class _FakeOutbox:
    def __init__(self, open_rows=()):
        self.rows = [dict(r) for r in open_rows]
        self.added = []
        self.armed = {}
        self._id = 100

    def arm_auto_send(self, item_id, when_iso):
        """Auto-send (2026-09-15): every released draft must be armed, or it silently waits
        forever for a tap Alex was told he no longer has to give."""
        self.armed[item_id] = when_iso
        for r in self.rows:
            if r["id"] == item_id:
                r["auto_send_at"] = when_iso
                return r
        return None

    def open_items(self, limit=60):
        return list(self.rows)

    def add(self, kind, title, *, detail="", link="", steps=None, account="", ref=""):
        # the real outbox collapses duplicate refs onto the existing open item
        for r in self.rows:
            if r.get("ref") == ref:
                return r["id"]
        self._id += 1
        self.rows.append({"id": self._id, "kind": kind, "title": title, "ref": ref})
        self.added.append(ref)
        return self._id


def _entry(n, draft_id="d%s", **kw):
    e = {"brand": f"Brand{n}", "to": f"founder{n}@brand{n}.com",
         "subject": "s", "body": "b", "draft_id": draft_id % n if "%s" in draft_id else draft_id}
    e.update(kw)
    return e


@pytest.fixture(autouse=True)
def _no_live_followup_budget(monkeypatch):
    """First touches now share the daily cap with follow-ups, so the release reads the real
    tracker to see how much of today's budget is already claimed. That made every release test
    depend on today's live follow-up schedule — with 23 due, the cap left room for zero and the
    tests failed for a reason that had nothing to do with what they were testing.

    Pinned to 0 here so each test controls only its own variable; the tests that are ABOUT the
    budget override it."""
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 0)


@pytest.fixture
def quiet_log(monkeypatch):
    """Capture log lines instead of appending to the real splitframe_daily.log."""
    lines = []
    monkeypatch.setattr(sfd, "log", lines.append)
    return lines


def test_the_five_a_day_cap_is_per_day_not_per_invocation(monkeypatch, quiet_log):
    """A retry, a launchd overlap or one manual run used to release another five on top of the
    five already waiting — the exact pile of notifications the cadence exists to prevent."""
    queue = [_entry(n) for n in range(1, 8)]          # seven written drafts
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    # Pin the cadence: what is under test is "per day, not per invocation", not today's
    # position on the ramp. Reading the live record here made the test depend on the tracker.
    monkeypatch.setattr(sfd, "current_cap", lambda: (5, "pinned"))
    box = _FakeOutbox()

    first = sfd.release_first_touches(box, "https://mail")
    assert len(first) == 5, f"first run should release exactly PER_DAY, got {first}"
    assert len(box.armed) == 5, "every released first touch must be armed to send itself"

    # same calendar day, second invocation: nothing more goes out
    assert sfd.release_first_touches(box, "https://mail") == []
    assert len(box.added) == 5

    # the two survivors are untouched and still pending
    assert sum(1 for e in queue if not e.get("released")) == 2

    # roll the clock: yesterday's five no longer spend today's budget
    for e in queue:
        if e.get("released"):
            e["released"] = (datetime.now(sfd.LOCAL_TZ) - timedelta(days=1)).isoformat()
    assert len(sfd.release_first_touches(box, "https://mail")) == 2
    assert all(e.get("released") for e in queue)


def test_a_conflicting_draft_defers_a_first_touch_it_does_not_retire_it(monkeypatch, quiet_log):
    """An unrelated open draft to the same address used to stamp the queued first touch
    'skipped: already waiting' forever: a written cold email that silently never went out."""
    queue = [_entry(1)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    box = _FakeOutbox([{"id": 1, "kind": "email_draft",
                        "title": "Send the reply to founder1@brand1.com", "ref": "gmail:studio:OTHER"}])

    assert sfd.release_first_touches(box, "https://mail") == []
    assert not queue[0].get("released"), "a deferral must not mark the entry released"
    assert any("held" in ln for ln in quiet_log), "a held first touch must be reported"

    # once the conflicting item clears, the queued draft goes out on the next run
    box.rows.clear()
    assert sfd.release_first_touches(box, "https://mail") == ["Brand1"]
    assert box.added == ["gmail:studio:d1"]


def test_a_queue_entry_with_no_draft_id_is_never_announced_as_sendable(monkeypatch, quiet_log):
    """splitframe_send.py refuses a ref with no draft id, so releasing one put 'ready to send'
    in front of Alex for an email the one-tap path could not send. Worse, every malformed entry
    shared the ref 'gmail:studio:' and collapsed onto one outbox row."""
    queue = [_entry(1, draft_id=""), _entry(2, draft_id=""), _entry(3)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    box = _FakeOutbox()

    assert sfd.release_first_touches(box, "https://mail") == ["Brand3"]
    assert box.added == ["gmail:studio:d3"], "a ref with an empty draft id must never be filed"
    assert not queue[0].get("released") and not queue[1].get("released")
    assert any("no draft_id" in ln for ln in quiet_log), "malformed entries must be reported"


def test_auto_send_only_picks_up_drafts_nobody_has_handled():
    """Auto-send is Alex being hands-off, not a second send of something already handled."""
    import outbox as ob
    rows = [
        {"id": 1, "kind": "email_draft", "auto_send_at": "2026-09-15T10:00:00"},           # due
        {"id": 2, "kind": "email_draft", "auto_send_at": "2026-09-15T23:00:00"},           # held
        {"id": 3, "kind": "email_draft"},                                                   # not armed
        {"id": 4, "kind": "email_draft", "auto_send_at": "2026-09-15T10:00:00",
         "send_approved": "x"},                                                             # he tapped
        {"id": 5, "kind": "email_draft", "auto_send_at": "2026-09-15T10:00:00",
         "sent_at": "x"},                                                                   # already gone
        {"id": 6, "kind": "task", "auto_send_at": "2026-09-15T10:00:00"},                   # not an email
    ]
    old = ob.open_items
    ob.open_items = lambda limit=60, include_snoozed=True: rows
    try:
        due = [it["id"] for it in ob.due_to_auto_send("2026-09-15T12:00:00")]
    finally:
        ob.open_items = old
    assert due == [1], f"expected only the due, unhandled email draft; got {due}"


def test_sender_counts_todays_sends_from_its_own_log(tmp_path, monkeypatch):
    """The cap is the blast radius of any bug in the drafter. A Mac that slept through three days
    of drafts must not wake up and fire all of them into one morning."""
    import importlib.util as iu
    spec = iu.spec_from_file_location("sfs2", SENDER)
    sfs = iu.module_from_spec(spec)
    spec.loader.exec_module(sfs)
    from datetime import date
    today = date.today().isoformat()
    log = tmp_path / "send.log"
    log.write_text(
        f"{today} 09:01 item 1: SENT to a@b.com (draft r1) — automatically (hold window expired)\n"
        f"{today} 09:02 item 2: SENT to c@d.com (draft r2) — on Alex's approval\n"
        f"{today} 09:03 item 3: send refused: recipient not in tracker\n"
        "2026-01-01 09:04 item 9: SENT to old@x.com (draft r9) — on Alex's approval\n")
    monkeypatch.setattr(sfs, "LOG", str(log))
    assert sfs._sent_today() == 2
    assert sfs.DAILY_CAP == 5


def test_the_do_page_says_an_armed_email_sends_itself():
    """Auto-send (2026-09-15) turned the nudge into a veto but left this page reading
    "Send it now". That is the one screen where Alex reads the actual email, so it is the
    screen where he'd decide 'not this one', close the tab, and have it go anyway three
    hours later. Doing nothing here is a yes now, and the page has to say so."""
    import do_actions
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    soon = (datetime.now(tz) + timedelta(hours=3)).replace(microsecond=0)

    class FakeOutbox:
        OPEN = "open"

        def __init__(self, item):
            self.item = item

        def get(self, _id):
            return self.item

        @staticmethod
        def summary_line(it):
            return it["title"] + " — 2h"

    armed = {"id": 1, "kind": "email_draft", "status": "open", "title": "Send the reply to a@b.com",
             "detail": "Subject: hi\n\nbody", "auto_send_at": soon.isoformat()}
    plain = {k: v for k, v in armed.items() if k != "auto_send_at"}

    old_ob, old_tz = do_actions.outbox_mod, do_actions.LOCAL_TZ
    do_actions.LOCAL_TZ = tz
    try:
        do_actions.outbox_mod = FakeOutbox(armed)
        view = {}
        do_actions._resolve_outbox(view, "1")
        steps = " ".join(view["steps"])
        clock = soon.strftime("%-I:%M %p")
        assert clock in steps, f"the send time must be on the page: {steps}"
        assert "sends itself" in steps
        assert "Not doing it" in steps, "the page must name the button that stops it"
        assert "Send it now" not in steps, "an armed draft must not ask for a tap it won't wait for"
        assert clock in view["why"] and "unless you stop it" in view["why"]

        # A 3-hour window started in the evening lands tomorrow: "sends itself at 12:56 AM"
        # with no day reads as "already gone". The weekday has to be there.
        tomorrow = (datetime.now(tz) + timedelta(days=1)).replace(microsecond=0)
        do_actions.outbox_mod = FakeOutbox({**armed, "auto_send_at": tomorrow.isoformat()})
        view_t = {}
        do_actions._resolve_outbox(view_t, "1")
        assert tomorrow.strftime("%a") in " ".join(view_t["steps"]), \
            "a send that crosses midnight must carry its weekday"

        # an UNARMED draft still waits for him, and must still say so
        do_actions.outbox_mod = FakeOutbox(plain)
        view2 = {}
        do_actions._resolve_outbox(view2, "1")
        steps2 = " ".join(view2["steps"])
        assert "Send it now" in steps2, "a draft with no auto-send still needs the tap"
        assert "sends itself" not in steps2
    finally:
        do_actions.outbox_mod, do_actions.LOCAL_TZ = old_ob, old_tz


def test_followups_reach_front_desk_brands():
    """The bug this pins: due_followups keyed on the `email` column, which is EMPTY for a brand
    whose only address is a front desk (that lives in email_generic). Every one of them was
    dropped before any date was even looked at — a first touch went out and no follow-up could
    ever fire. Silent, and after 2026-09-17 most of the funnel is those brands."""
    today = sfd.date(2026, 9, 20)
    rows = [
        {"brand": "Named", "email": "eric@bigbarker.com", "email_generic": "",
         "contact_name": "", "sent_date": "2026-09-16", "replied": "", "outcome": "",
         "followup1_date": "2026-09-19", "followup2_date": "2026-09-23"},
        {"brand": "FrontDesk", "email": "", "email_generic": "hello@calypsa.com",
         "contact_name": "", "sent_date": "2026-09-16", "replied": "", "outcome": "",
         "followup1_date": "2026-09-19", "followup2_date": "2026-09-23"},
        {"brand": "TicketDesk", "email": "", "email_generic": "support@zitsticka.com",
         "contact_name": "", "sent_date": "2026-09-16", "replied": "", "outcome": "",
         "followup1_date": "2026-09-19", "followup2_date": "2026-09-23"},
    ]
    due = sfd.due_followups(rows, today, {})
    assert [r["brand"] for r, _touch in due] == ["Named", "FrontDesk"]
    assert all(touch == 2 for _r, touch in due)

    # And the address it would actually write to is the front desk, not "".
    assert sfd.target_address(rows[1]) == ("hello@calypsa.com", "shared")

    # A brand that answered is still left alone, whichever column held the address.
    rows[1]["replied"] = "2026-09-18"
    assert [r["brand"] for r, _t in sfd.due_followups(rows, today, {})] == ["Named"]


def test_first_touch_waiting_list_counts_front_desks_too():
    rows = [{"brand": "FrontDesk", "email": "", "email_generic": "hello@calypsa.com",
             "contact_name": "", "sent_date": ""},
            {"brand": "Ticket", "email": "support@x.com", "email_generic": "",
             "contact_name": "", "sent_date": ""},
            {"brand": "Sent", "email": "eric@bigbarker.com", "email_generic": "",
             "contact_name": "", "sent_date": "2026-09-16"}]
    assert [r["brand"] for r in sfd.waiting_for_first_touch(rows)] == ["FrontDesk"]


# ---------------------------------------------------------------------------
# The daily cap. 5/day was quietly the binding constraint on the business — it
# put the volume a first client needs past the kill date — and it stayed at 5
# because raising it was a judgement nobody was scheduled to make. These pin
# that the cadence now earns its way up from the delivery record, and drops
# back to the floor on the first sign of trouble without anyone deciding to.
# ---------------------------------------------------------------------------

def test_daily_cap_ramps_with_clean_send_history():
    assert sfd.daily_cap(0, 0, 0) == 5
    assert sfd.daily_cap(19, 0, 19) == 5
    assert sfd.daily_cap(20, 0, 20) == 10
    assert sfd.daily_cap(59, 0, 59) == 10
    assert sfd.daily_cap(60, 0, 60) == 15
    assert sfd.daily_cap(119, 0, 119) == 15
    assert sfd.daily_cap(120, 0, 120) == 20


def test_daily_cap_has_a_hard_ceiling():
    """Workspace allows 2,000/day; that is never the binding number. Alex raised the ceiling to
    20 on 2026-09-18 after the reputation risk was named to him. It is still a CEILING — the
    cap must not keep climbing with volume."""
    assert sfd.daily_cap(10_000, 0, 10_000) == 20
    assert max(cap for _thr, cap in sfd.RAMP) == 20


def test_bounce_trouble_drops_the_cap_back_to_the_floor():
    # 2 bounces in 25 sends = 8%, exactly the threshold the bounce nudge fires on.
    assert sfd.daily_cap(200, 2, 25) == 5
    # One bounce is not a pattern; a single bad address must not stall the pipeline.
    assert sfd.daily_cap(200, 1, 25) == 20
    # Nor is a high count against a large denominator below the rate.
    assert sfd.daily_cap(200, 3, 200) == 20
    # The drop is from the TOP of the ramp too: high volume is no defence against bad delivery.
    assert sfd.daily_cap(5_000, 40, 200) == 5


def test_unreadable_delivery_state_falls_back_to_the_floor(monkeypatch):
    """An unreadable bounce record must never read as 'no bounces, send more'."""
    monkeypatch.setattr(sfd, "_sent_counts", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    cap, why = sfd.current_cap()
    assert cap == sfd.PER_DAY
    assert "floor" in why


def test_no_send_history_holds_at_the_floor(monkeypatch):
    """sent_since returning 0 because a module was never init'd used to look exactly like
    'no sends yet' — silent, and wrong in the direction that looks safe."""
    monkeypatch.setattr(sfd, "_sent_counts", lambda *a, **k: (0, 0))
    cap, why = sfd.current_cap()
    assert cap == sfd.PER_DAY
    assert "no send history" in why


def test_release_uses_the_live_cap_when_no_limit_is_passed(monkeypatch):
    """The release defaults to the earned cadence, not to the floor constant."""
    import inspect
    sig = inspect.signature(sfd.release_first_touches)
    assert sig.parameters["limit"].default is None
    src = inspect.getsource(sfd.release_first_touches)
    assert "current_cap()" in src


# ---------------------------------------------------------------------------
# log() must never be the reason a run dies. It was unguarded and pointed under
# ~, so on the server (HOME=/root) every call raised FileNotFoundError and took
# the whole follow-up-and-release run with it — reported only as a generic
# "follow-up drafting failed" warning nobody read. It went total when a cap line
# was added to release_first_touches; before that it only died on days a
# follow-up was actually due, which is almost certainly why FU1 Sep 3, FU2 Sep 8
# and FU1 Sep 14 were all "missed".
# ---------------------------------------------------------------------------

def test_log_survives_an_unwritable_path(monkeypatch, capsys):
    monkeypatch.setattr(sfd, "LOG", "/nonexistent-dir-for-tests/splitframe.log")
    sfd.log("this must not raise")          # the assertion is that it returns at all
    assert "this must not raise" in capsys.readouterr().out, "stdout is the line that matters"


def test_log_path_is_beside_the_module_not_under_home():
    """The server container's HOME is /root and it runs the repo elsewhere, so any ~-based
    path is a file that cannot be created."""
    assert "~" not in sfd.LOG and "/root" not in sfd.LOG
    assert sfd.LOG.endswith("scripts/splitframe_daily.log")


def test_releasing_survives_a_dead_logger(monkeypatch):
    """The regression in one line: release_first_touches logs its cap before doing anything,
    so a logger that can throw meant the raised cap released nothing at all."""
    queue = [_entry(n) for n in range(1, 4)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "LOG", "/nonexistent-dir-for-tests/splitframe.log")
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    assert len(sfd.release_first_touches(_FakeOutbox(), "https://mail")) == 3


def test_local_state_write_is_a_fallback_not_a_second_failure(monkeypatch):
    """save_state's local file catches a Supabase failure, so it must not crash harder than
    what it caught. Unguarded it shared LOG's defect: the path does not exist on the server,
    so a Supabase hiccup became a FileNotFoundError that killed the release outright."""
    monkeypatch.setattr(sfd, "_shared", None)
    monkeypatch.setattr(sfd, "STATE", "/nonexistent-dir-for-tests/state.json")
    monkeypatch.setattr(sfd, "LOG", "/nonexistent-dir-for-tests/splitframe.log")
    sfd.save_state({"drafted": {}})          # must return, not raise


def test_state_path_is_beside_the_module_not_under_home():
    assert "~" not in sfd.STATE and "/root" not in sfd.STATE


# ---------------------------------------------------------------------------
# Stale drafts. A queued first touch states facts about a LIVE ad account, and
# those facts rot: Ironcroft went 8 active ads -> 0 in the two days between the
# read and the draft. The pitch only works because Alex says things a founder
# can check and find true.
# ---------------------------------------------------------------------------

def test_a_draft_older_than_the_limit_is_held_not_sent(monkeypatch, quiet_log):
    old = (datetime.now(sfd.LOCAL_TZ) - timedelta(days=sfd.STALE_DRAFT_DAYS + 1)).isoformat()
    queue = [dict(_entry(1), queued_at=old), _entry(2)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    out = sfd.release_first_touches(_FakeOutbox(), "https://mail")
    assert out == ["Brand2"], "the aged draft must not go out"
    assert any("HELD" in line for line in quiet_log)


def test_a_fresh_draft_is_unaffected(monkeypatch, quiet_log):
    fresh = (datetime.now(sfd.LOCAL_TZ) - timedelta(hours=6)).isoformat()
    queue = [dict(_entry(1), queued_at=fresh)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    assert sfd.release_first_touches(_FakeOutbox(), "https://mail") == ["Brand1"]


def test_an_unknown_age_never_blocks_a_send(monkeypatch, quiet_log):
    """Only a KNOWN old draft is held. A missing or unparseable queued_at must not quietly
    stop the funnel — that would be a silent halt dressed as a safety feature."""
    assert sfd._queued_age_days({}) == 0.0
    assert sfd._queued_age_days({"queued_at": "not-a-date"}) == 0.0
    queue = [dict(_entry(1), queued_at=""), dict(_entry(2), queued_at="garbage")]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    assert sfd.release_first_touches(_FakeOutbox(), "https://mail") == ["Brand1", "Brand2"]


def test_nudge_wires_proactive_on_the_mac_without_clobbering_a_real_init(monkeypatch):
    """The nudge is how Alex vetoes a send inside the 3 h window, so it must not be best-effort.
    On the Mac proactive was never initialised, so every send_nudge raised and fell through to
    raw ntfy — delivering, but without dedup or re-nudging."""
    import types
    fake = types.ModuleType("proactive")
    fake.intake_mod = None
    fake.LOCAL_TZ = None
    calls = {}
    fake.send_nudge = lambda *a, **k: calls.setdefault("sent", True) and ""
    monkeypatch.setitem(__import__("sys").modules, "proactive", fake)
    monkeypatch.setattr(sfd, "_shared", object())
    sfd.nudge("t", "b")
    assert fake.intake_mod is sfd._shared, "should wire the intake module it actually needs"
    assert fake.LOCAL_TZ is sfd.LOCAL_TZ

    # A module already initialised (the server) must be left exactly as it is.
    real_intake, real_tz = object(), object()
    fake.intake_mod, fake.LOCAL_TZ = real_intake, real_tz
    sfd.nudge("t", "b")
    assert fake.intake_mod is real_intake and fake.LOCAL_TZ is real_tz


# ---------------------------------------------------------------------------
# First touches and follow-ups share ONE daily cap, because the cap is what the
# sending domain experiences. The release used to ask for the whole cap as if
# it owned it, so on 2026-09-20 five follow-ups and five first touches filled
# it by 11:05 and ten more sat deferred all evening — and a first touch that
# waits long enough is HELD as stale, so the work is thrown away, not sent late.
# ---------------------------------------------------------------------------

def _fu_row(fu1="", fu2="", replied="", outcome=""):
    return {"brand": "B", "sent_date": "2026-09-01", "email": "a@b.com", "email_generic": "",
            "contact_name": "", "followup1_date": fu1, "followup2_date": fu2,
            "replied": replied, "outcome": outcome}


def test_followups_due_counts_only_touches_that_will_actually_send(monkeypatch):
    monkeypatch.undo()                     # this test IS about the real counter
    today = date(2026, 9, 21)
    rows = [
        _fu_row(fu1="2026-09-21"),                       # due today
        _fu_row(fu1="2026-09-15"),                       # overdue, still owed
        _fu_row(fu1="2026-09-30"),                       # not yet
        _fu_row(fu1="2026-09-21", replied="2026-09-19"), # answered: never chased
        _fu_row(fu1="2026-09-21", outcome="no_response"),# closed out
    ]
    assert sfd.followups_due_today(rows, today) == 2


def test_one_touch_per_prospect_even_when_both_are_overdue(monkeypatch):
    monkeypatch.undo()                     # this test IS about the real counter
    """due_followups sends one touch per prospect per run; the budget must count the same way
    or it reserves capacity that will not be used."""
    rows = [_fu_row(fu1="2026-09-10", fu2="2026-09-14")]
    assert sfd.followups_due_today(rows, date(2026, 9, 21)) == 1


def test_follow_ups_no_longer_eat_the_first_touch_cap(monkeypatch, quiet_log):
    """Decision A (2026-09-23): the cap counts first touches only. Six follow-ups due used to
    leave four first touches; now all eight queued go, because 10 + 6 is under the ceiling."""
    queue = [_entry(n) for n in range(1, 9)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 6)
    out = sfd.release_first_touches(_FakeOutbox(), "https://mail")
    assert len(out) == 8
    assert any("on their own budget" in line for line in quiet_log)


def test_the_ceiling_still_bounds_first_touches_after_the_days_follow_ups(monkeypatch, quiet_log):
    """14 follow-ups due under a ceiling of 20 leaves room for 6 first touches, cap or not —
    releasing more would queue drafts the sender cannot send today and they would go stale."""
    queue = [_entry(n) for n in range(1, 9)]
    monkeypatch.setattr(sfd, "_shared", _FakeShared(queue))
    monkeypatch.setattr(sfd, "effective_ceiling", lambda now=None: (20, "pinned"))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 14)
    assert len(sfd.release_first_touches(_FakeOutbox(), "https://mail")) == 6


def test_follow_ups_can_still_take_the_whole_day_at_the_ceiling(monkeypatch, quiet_log):
    """22 due (the real 2026-09-23 number) is past the ceiling: no first touch today, said so."""
    monkeypatch.setattr(sfd, "_shared", _FakeShared([_entry(1)]))
    monkeypatch.setattr(sfd, "effective_ceiling", lambda now=None: (20, "pinned"))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 22)
    assert sfd.release_first_touches(_FakeOutbox(), "https://mail") == []
    assert any("reach the daily ceiling" in line for line in quiet_log)


def test_the_raised_ceiling_leaves_room_for_more_first_touches(monkeypatch, quiet_log):
    """2026-09-24: 17 follow-ups due. At 20 that left 3 first touches; at 25 it leaves 8."""
    monkeypatch.setattr(sfd, "_shared", _FakeShared([_entry(n) for n in range(1, 12)]))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "effective_ceiling", lambda now=None: (25, "no bounce in 48 h"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 17)
    assert len(sfd.release_first_touches(_FakeOutbox(), "https://mail")) == 8
    assert any("ceiling of 25 (no bounce in 48 h)" in line for line in quiet_log)


def _bounce(hours_ago):
    return {"at": (datetime.now(sfd.LOCAL_TZ) - timedelta(hours=hours_ago)).isoformat(), "to": "x@y.com"}


def test_a_bounce_holds_the_ceiling_at_20_for_48_hours():
    now = datetime.now(sfd.LOCAL_TZ)
    assert sfd.TOTAL_DAILY_CEILING == 25 and sfd.CEILING_AFTER_BOUNCE == 20
    assert sfd.ceiling_for([], now)[0] == 25
    assert sfd.ceiling_for([_bounce(1)], now)[0] == 20
    assert sfd.ceiling_for([_bounce(47)], now)[0] == 20, "one day clean is not enough"
    assert sfd.ceiling_for([_bounce(49)], now)[0] == 25, "48 h clean brings it back"
    assert sfd.ceiling_for([_bounce(49), _bounce(2)], now)[0] == 20
    naive = {"at": (datetime.now() - timedelta(hours=3)).replace(tzinfo=None).isoformat()}
    assert sfd.ceiling_for([naive], now)[0] == 20, "a naive local timestamp still counts"


def test_an_unreadable_bounce_record_never_reads_as_the_raised_ceiling(monkeypatch):
    def boom():
        raise RuntimeError("supabase down")
    monkeypatch.setattr(sfd, "_bounce_events", boom)
    ceiling, why = sfd.effective_ceiling()
    assert ceiling == 20 and "unreadable" in why


def test_the_switch_restores_the_shared_cap(monkeypatch, quiet_log):
    """FOLLOWUPS_SHARE_CAP = True is the one-line reversal of decision A: 10 cap minus 6
    follow-ups leaves 4, and 14 due fills the day."""
    monkeypatch.setattr(sfd, "FOLLOWUPS_SHARE_CAP", True)
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "_shared", _FakeShared([_entry(n) for n in range(1, 9)]))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 6)
    assert len(sfd.release_first_touches(_FakeOutbox(), "https://mail")) == 4
    monkeypatch.setattr(sfd, "_shared", _FakeShared([_entry(1)]))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 14)
    assert sfd.release_first_touches(_FakeOutbox(), "https://mail") == []
    assert any("follow-ups alone fill the cap" in line for line in quiet_log)


def test_the_ceiling_is_a_real_bound_above_the_cap():
    assert sfd.TOTAL_DAILY_CEILING >= max(cap for _t, cap in sfd.RAMP) or sfd.TOTAL_DAILY_CEILING >= 20
    assert sfd.FOLLOWUPS_SHARE_CAP is False


def test_an_unreadable_tracker_does_not_silently_stop_first_touches(monkeypatch):
    """Failing to 0 means the release proceeds at full cap. The opposite default would stop the
    funnel on a file-read error and look exactly like a quiet day."""
    monkeypatch.undo()
    monkeypatch.setattr(sfd, "TRACKER", "/nonexistent-dir-for-tests/tracker.csv")
    assert sfd.followups_due_today() == 0


def test_revised_draft_is_not_stale():
    """A revise re-reads the facts, so the stale clock restarts at revised_at."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import splitframe_daily as sd
    from datetime import datetime, timedelta
    old = (datetime.now(sd.LOCAL_TZ) - timedelta(days=5)).isoformat()
    fresh = datetime.now(sd.LOCAL_TZ).isoformat()
    assert sd._queued_age_days({"queued_at": old}) > sd.STALE_DRAFT_DAYS
    assert sd._queued_age_days({"queued_at": old, "revised_at": fresh}) < 1


# ---- 2026-09-25: follow-ups that already went must not starve the release ----

_REAL_DUE = sfd.followups_due_today        # captured before the autouse fixture pins it to 0


class _KeyedShared:
    """Shared state that keeps each key apart: the release reads the queue AND the follow-up
    state, and _FakeShared hands back the same dict for every key."""
    def __init__(self, states):
        self.states = states

    def _load_state(self, key):
        return self.states.get(key)

    def _save_state(self, st):
        self.states[st["key"]] = st


_OPEN = [
    {"id": 1, "kind": "email_draft", "account": "studio", "detail": "Subject: Re: hi\n\nbody"},
    {"id": 2, "kind": "email_draft", "account": "studio", "detail": "Subject: RE: two\n\nx"},
    {"id": 3, "kind": "email_draft", "account": "studio", "detail": "Subject: Six ads\n\nx"},
    {"id": 4, "kind": "email_draft", "account": "personal", "detail": "Subject: Re: dinner\n\nx"},
    {"id": 5, "kind": "email_draft", "account": "studio", "detail": "Subject: Re: gone\n\nx",
     "sent_at": "2026-09-25T09:00:00"},
    {"id": 6, "kind": "task", "detail": "Subject: Re: not an email"},
]


def test_open_follow_up_drafts_are_counted_once_and_only_the_studios():
    """Written, not yet sent, in the studio mailbox: two here. A first touch, a reply CLARVIS
    drafted for Alex's own inbox, and one already sent are not today's follow-up budget."""
    assert sfd.open_follow_ups(_FakeOutbox(_OPEN)) == 2


def test_a_dead_outbox_counts_no_open_follow_ups():
    class Dead:
        @staticmethod
        def open_items(limit=60):
            raise RuntimeError("supabase down")
    assert sfd.open_follow_ups(Dead) == 0


def test_a_follow_up_already_drafted_is_not_due_again():
    """The counter used to ignore the drafted state and count every prospect whose follow-up
    DATE had passed, sent or not. `drafted` is what due_followups already skips on."""
    today = date(2026, 9, 25)
    rows = [dict(_fu_row(fu1="2026-09-20"), email="a@x.com"),                  # due, not drafted
            dict(_fu_row(fu1="2026-09-20"), email="b@x.com"),                  # drafted (waiting)
            dict(_fu_row(fu1="2026-09-20", fu2="2026-09-24"), email="c@x.com"),  # FU1 went, FU2 due
            dict(_fu_row(fu1="2026-09-10", fu2="2026-09-14"), email="d@x.com")]  # both went
    drafted = {"b@x.com": [2], "c@x.com": [2], "d@x.com": [2, 3]}
    assert _REAL_DUE(rows, today, {}) == 4, "with no drafted state it is the old count"
    assert _REAL_DUE(rows, today, drafted) == 2                # a (touch 2) and c (touch 3)
    assert _REAL_DUE(rows, today, drafted, _FakeOutbox(_OPEN)) == 4   # + the two waiting drafts


def test_the_release_is_not_starved_by_follow_ups_that_already_went(monkeypatch, quiet_log, tmp_path):
    """The 2026-09-25 bug. 52 prospects past their follow-up dates, every touch already drafted
    and sent. The old count called all 52 due, and under a ceiling of 20 that left room for 0
    first touches: nothing was released from 09-22 on. Now only the 3 follow-up drafts still
    waiting in the outbox take room, so 20 - 3 leaves 17 and the cap of 10 is the bound."""
    past = (datetime.now(sfd.LOCAL_TZ).date() - timedelta(days=10)).isoformat()
    tracker = tmp_path / "prospect-tracker.csv"
    rows = [dict(_fu_row(fu1=past, fu2=past), email=f"p{n}@x.com") for n in range(52)]
    with open(tracker, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    monkeypatch.setattr(sfd, "TRACKER", str(tracker))
    monkeypatch.setattr(sfd, "followups_due_today", _REAL_DUE)
    queue = [_entry(n) for n in range(1, 13)]
    monkeypatch.setattr(sfd, "_shared", _KeyedShared({
        sfd.QUEUE_KEY: {"key": sfd.QUEUE_KEY, "queue": queue},
        sfd.STATE_KEY: {"key": sfd.STATE_KEY, "drafted": {f"p{n}@x.com": [2, 3] for n in range(52)}},
    }))
    monkeypatch.setattr(sfd, "effective_ceiling", lambda now=None: (20, "pinned"))
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    waiting = [{"id": 900 + i, "kind": "email_draft", "account": "studio", "title": f"Send the reply to w{i}@y.com",
                "detail": "Subject: Re: earlier\n\nx", "auto_send_at": "2026-09-25T10:00:00"} for i in range(3)]
    out = sfd.release_first_touches(_FakeOutbox(waiting), "https://mail")
    assert len(out) == 10
    assert any("3 follow-up(s) due today" in line for line in quiet_log), quiet_log
