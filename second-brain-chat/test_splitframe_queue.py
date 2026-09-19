"""Tests for scripts/splitframe_queue.py — the nightly money shift's way into the first-touch
queue. No network: the tracker, the queue and Gmail are all stubbed."""
import importlib.util
import os
import sys
import types

import pytest

PATH = os.path.expanduser("~/second-brain/scripts/splitframe_queue.py")
SPEC = importlib.util.spec_from_file_location("splitframe_queue", PATH)
sq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sq)

TODAY = "2026-09-16"

GOOD_BODY = " ".join(["word"] * 118)     # 118 words, no tells

GOOD_BODY_PROSE = (
    "Nineteen active ads and every one of them is the same product shot on the same white "
    "background. The caption changes, the picture never does. Nothing shows the collagen "
    "actually being used, nothing shows a before and after, and nothing speaks to someone "
    "who has tried three other brands already. That last group is most of who buys this. "
    "What I would test first is one ad that opens on the mixing, not the tub, and one that "
    "leads with the price per serving against the two brands people switch from. Cheap to "
    "make, easy to read in the numbers within a week. I make ad creative for DTC brands, "
    "statics and short scripts, delivered in a couple of days. Want me to send a sample "
    "built on your account so you can see what I mean?"
)


def _row(**kw):
    base = {"brand": "Obvi", "domain": "myobvi.com", "email": "ankit@myobvi.com",
            "status": "qualified", "sent_date": "", "replied": "", "outcome": "",
            "contact_name": "Ankit Patel", "adlib_url": "https://www.facebook.com/ads/library/?q=Obvi",
            "notes": "adlib 19 active [read live 2026-09-16] · page=Obvi id=2431731276838642"}
    base.update(kw)
    return base


def test_no_send_capability():
    """Same pin as the daily job: this feeds a queue that sends itself, so the script that
    feeds it must never be able to send."""
    src = open(PATH).read()
    for forbidden in ("GMAIL_SEND_EMAIL", "GMAIL_SEND", "send_email", "smtplib", "SEND_DRAFT",
                      "approve_send", "arm_auto_send"):
        assert forbidden not in src, f"a send/arm path appeared in splitframe_queue: {forbidden}"


def test_reads_count_and_page_id_from_notes():
    assert sq.known_count(_row()) == (19, "2026-09-16")
    assert sq.known_count(_row(notes="adlib 96 active, 27/28 visible 30d+; page=Wild One id=144665")) == (96, None)
    assert sq.known_count(_row(notes="")) == (None, None)
    assert sq.page_id(_row()) == "2431731276838642"
    assert sq.page_id(_row(notes="nothing here")) == ""


def test_bands():
    assert sq.band(None) == "unknown"
    assert sq.band(0) == "zero"
    assert sq.band(5) == "in" and sq.band(50) == "in"
    assert sq.band(75) == "out"
    assert sq.band(110) == "big"


def test_next_targets_filters_and_orders():
    """A named person outranks a front desk at a better-fitting brand, because who reads it
    moves the reply rate more than five ads either way does. Inside a tier, band decides."""
    rows = [
        _row(),                                                              # person, in band
        _row(brand="Emi Jay", email="julianne@emijay.com", notes="adlib 84 active; id=4542"),   # person, out
        _row(brand="Fresh", email="hello@fresh.com", notes="page=Fresh id=1234567"),            # desk, unknown
        _row(brand="Momentous", email="jeff@livemomentous.com", notes="adlib 110 active [read live 2026-09-16]"),
        _row(brand="Wild One", email="bill@wildone.com", notes="adlib 0 active [read live 2026-09-16]"),
        _row(brand="Sent", email="sent@x.com", sent_date="2026-09-15"),
        _row(brand="Replied", email="r@x.com", replied="2026-09-15"),
        _row(brand="Support", email="support@zitsticka.com"),
        _row(brand="Cand", email="c@x.com", status="candidate"),
        _row(brand="Queued", email="q@x.com"),
    ]
    queue = [{"to": "q@x.com", "released": "2026-09-15T13:00:00-04:00"}]
    got = sq.next_targets(rows, queue)
    assert [t["brand"] for t in got] == ["Obvi", "Emi Jay", "Fresh"]
    assert [t["tier"] for t in got] == ["person", "person", "shared"]
    assert got[0]["page_id"] == "2431731276838642"
    assert got[0]["band"] == "in" and got[1]["band"] == "out" and got[2]["band"] == "unknown"


def test_address_tiers():
    """Person needs positive evidence; anything else that is not a ticket queue is a front desk.

    The old rule was one blocklist of role prefixes, so every address that was not on it counted
    as a person — `goodday@`, `store@` and `oudwarellc@` would all have been greeted by name.
    """
    named = _row(email="maxx.appelman@trulybeauty.com", contact_name="Maxx Appelman")
    assert sq.target_address(named) == ("maxx.appelman@trulybeauty.com", "person")
    assert sq.target_address(_row(email="pveksler@universalstandard.com",
                                  contact_name="Polina Veksler"))[1] == "person"
    assert sq.target_address(_row(email="gillian@beautyfrombees.ca", contact_name=""))[1] == "person"

    # No name in the address: reachable, but nobody to greet.
    for addr in ("goodday@brightland.co", "justdoughit@twisteddough.shop",
                 "oudwarellc@oudware.com", "press@moonjuice.com", "hello@calypsa.com"):
        assert sq.target_address(_row(email=addr, contact_name=""))[1] == "shared", addr

    # A ticket queue is not a target at all.
    for addr in ("support@zitsticka.com", "store@primogolfapparel.com",
                 "wholesale@manduka.com", "sup@curiebod.com", "orders@x.com"):
        assert sq.target_address(_row(email=addr, contact_name="")) == ("", ""), addr

    # A person on the row beats a front desk on the same row, whichever column holds it.
    both = _row(email="hello@fishwife.com", email_generic="becca@fishwife.com", contact_name="")
    assert sq.target_address(both) == ("becca@fishwife.com", "person")

    # A contact_name that does not match any known address does not invent a person.
    assert sq.target_address(_row(email="hello@x.com", contact_name="Natasha Oakley"))[1] == "shared"


def test_candidates_and_hunter_targets():
    rows = [
        _row(brand="A", status="candidate", notes="page=A id=1111111"),
        _row(brand="B", status="candidate", notes="adlib 20 active [read live 2026-09-10] page=B id=2222222"),
        _row(brand="C", status="candidate", notes="adlib 20 active [read live 2026-07-01] page=C id=3333333"),
        _row(brand="D", status="candidate", notes="", adlib_url=""),
        _row(brand="E", status="qualified", email="", notes="adlib 30 active; id=5555555"),
        _row(brand="F", status="qualified", email="help@f.com", notes="adlib 61 active; id=6666666"),
        _row(brand="G", status="qualified", email="", notes="adlib 150 active [read live 2026-09-16]"),
    ]
    assert [c["brand"] for c in sq.candidates_to_qualify(rows, TODAY)] == ["A", "C"]
    hunters = sq.hunter_targets(rows)
    assert [h["brand"] for h in hunters] == ["E", "F"]
    assert hunters[1]["has_generic"] is True


def test_guard_body_catches_the_tells():
    assert sq.guard_body(GOOD_BODY_PROSE) == []
    assert any("too short" in p for p in sq.guard_body("way too short"))
    assert any("too long" in p for p in sq.guard_body(" ".join(["w"] * 200)))
    assert any("claims work" in p for p in sq.guard_body(GOOD_BODY + " I actually built the rescue concept already."))
    assert any("em dash" in p for p in sq.guard_body(GOOD_BODY + " a — b — c"))
    assert any('"AI"' in p for p in sq.guard_body(GOOD_BODY + " we use AI tools"))
    assert any("flourish" in p for p in sq.guard_body(GOOD_BODY + " Looking forward to hearing from you."))


def test_plan_add_refuses_everything_that_must_not_go_out():
    rows = [_row(), _row(brand="Sent", email="sent@x.com", sent_date="2026-09-15"),
            _row(brand="Support", email="support@zitsticka.com"),
            _row(brand="Desk", email="hello@desk.com", contact_name=""),
            _row(brand="Cand", email="c@x.com", status="candidate"),
            _row(brand="Replied", email="r@x.com", replied="2026-09-15")]
    queue = [{"to": "ankit@myobvi.com", "released": ""}]

    row, p = sq.plan_add(rows, [], "nobody@nowhere.com", "s", GOOD_BODY_PROSE, 19)
    assert row is None and "not in the tracker" in p[0]

    _, p = sq.plan_add(rows, queue, "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 19)
    assert any("already in the first-touch queue" in x for x in p)

    _, p = sq.plan_add(rows, [], "sent@x.com", "s", GOOD_BODY_PROSE, 19)
    assert any("already emailed" in x for x in p)
    _, p = sq.plan_add(rows, [], "support@zitsticka.com", "s", GOOD_BODY_PROSE, 19)
    assert any("ticket queue" in x for x in p)

    # A front desk is allowed through now — it is the only address most small brands publish.
    row, p = sq.plan_add(rows, [], "hello@desk.com", "s", GOOD_BODY_PROSE, 19)
    assert row is not None and p == []
    _, p = sq.plan_add(rows, [], "c@x.com", "s", GOOD_BODY_PROSE, 19)
    assert any("not qualified" in x for x in p)
    _, p = sq.plan_add(rows, [], "r@x.com", "s", GOOD_BODY_PROSE, 19)
    assert any("already replied" in x for x in p)

    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "", GOOD_BODY_PROSE, 19)
    assert any("no subject" in x for x in p)
    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, None)
    assert any("--ad-count is required" in x for x in p)
    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 0)
    assert any("0 active ads" in x for x in p)
    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 130)
    assert any("in-house team" in x for x in p)
    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 19, brand="Diggs")
    assert any("does not match" in x for x in p)

    row, p = sq.plan_add(rows, [], "Ankit@MyObvi.com", "19 ads, one product", GOOD_BODY_PROSE, 19, brand="obvi")
    assert p == [] and row["brand"] == "Obvi"


def test_stamp_count_changes_only_what_a_live_read_implies():
    notes, status, ch = sq.stamp_count(_row(notes="old note", status="candidate"), 20, TODAY)
    assert notes.startswith("adlib 20 active [read live 2026-09-16] · old note")
    assert status == "qualified" and "qualified" in " ".join(ch)
    notes, status, ch = sq.stamp_count(_row(notes="", status="qualified"), 0, TODAY)
    assert status == "hold"
    # a brand already emailed is never flipped to hold by a later read
    _, status, _ = sq.stamp_count(_row(status="qualified", sent_date="2026-09-15"), 0, TODAY)
    assert status == "qualified"
    # 100+ is noted, status untouched
    _, status, ch = sq.stamp_count(_row(status="qualified", notes=""), 120, TODAY)
    assert status == "qualified" and ch == ["noted 120 active"]
    # idempotent on the same day
    stamped = _row(notes="adlib 19 active [read live 2026-09-16] · x")
    assert sq.stamp_count(stamped, 19, TODAY)[2] == []


def test_plan_source_only_adds_brands_nobody_had_and_on_a_live_count():
    """New supply is the whole funnel now that the tracker is mined out, and the one way it
    goes wrong is a name entering qualified on a number nobody read."""
    rows = [_row()]
    row, problems = sq.plan_source(rows, "Obvi", "obvi-other.com", 20, TODAY)
    assert row is None and "already in the tracker" in problems[0]
    row, problems = sq.plan_source(rows, "Other Brand", "https://www.myobvi.com/collections", 20, TODAY)
    assert row is None and "already in the tracker as Obvi" in problems[0]
    row, problems = sq.plan_source(rows, "", "", None, TODAY)
    assert row is None and len(problems) == 3

    row, problems = sq.plan_source(rows, "Jolie", "www.Jolie.com/", 22, TODAY, pid="12345",
                                   category="Home (showerheads)", evidence="one product, five hooks")
    assert problems == [] and row["domain"] == "jolie.com" and row["status"] == "qualified"
    assert row["notes"].startswith("adlib 22 active [read live 2026-09-16] · sourced 2026-09-16")
    assert "id=12345" in row["notes"] and "one product, five hooks" in row["notes"]
    assert "view_all_page_id=12345" in row["adlib_url"] and row["email"] == ""
    # the count decides the status, exactly as a live `note` would
    assert sq.plan_source(rows, "Zero Co", "zeroco.com", 0, TODAY)[0]["status"] == "hold"
    assert sq.plan_source(rows, "Tiny", "tiny.com", 2, TODAY)[0]["status"] == "too_small"
    assert sq.plan_source(rows, "Giant", "giant.com", 130, TODAY)[0]["status"] == "too_big"
    assert sq.plan_source(rows, "Edge", "edge.com", 96, TODAY)[0]["status"] == "qualified"
    # no page id: the keyword search url, which is what was actually read
    assert "search_type=keyword_unordered" in sq.plan_source(rows, "No Page", "nopage.com", 9, TODAY)[0]["adlib_url"]


def _queue_of_three():
    return [{"to": "a@x.com", "brand": "A", "subject": "s", "released": ""},
            {"to": "b@x.com", "brand": "B", "subject": "s", "released": "2026-09-16T07:30:00-04:00"},
            {"to": "c@x.com", "brand": "C", "subject": "s", "released": "2026-09-15T07:30:00-04:00"}]


def test_status_report_runway_math(monkeypatch):
    # Pinned to a cadence rather than read from the live delivery record, so the arithmetic
    # under test is the queue's and not today's bounce count.
    monkeypatch.setattr(sq, "current_per_day", lambda: (5, "pinned"))
    rep = sq.status_report([_row()], _queue_of_three(), TODAY)
    assert rep["pending_count"] == 1 and rep["released_today"] == 1
    assert rep["runway_days"] == 0.2 and rep["need_drafts"] == 9
    assert [t["brand"] for t in rep["next_targets"]] == ["Obvi"]


def test_stock_target_tracks_the_cadence():
    """A hard-coded 10 would starve the queue the day the cap went to 8, and nothing would
    have said so until it ran dry."""
    assert sq.current_runway_target(5) == 10
    assert sq.current_runway_target(8) == 16
    assert sq.current_runway_target(10) == 20


def test_status_report_reads_the_live_cadence(monkeypatch):
    monkeypatch.setattr(sq, "current_per_day", lambda: (8, "30 sends, 0 bounces"))
    rep = sq.status_report([_row()], _queue_of_three(), TODAY)
    assert rep["per_day"] == 8
    assert rep["need_drafts"] == 15          # 2 x 8 in stock, 1 pending
    assert rep["runway_days"] == 0.1


def test_cadence_falls_back_to_the_floor_if_it_cannot_be_read(monkeypatch):
    def boom():
        raise RuntimeError("no state")
    monkeypatch.setattr(sq._sfd, "current_cap", boom)
    cap, why = sq.current_per_day()
    assert cap == sq.PER_DAY and "floor" in why


def _fake_gmail(monkeypatch, draft_id="r123"):
    """A stand-in mail_drafts + composio: records that outbox filing was switched off."""
    calls = {}
    md = types.ModuleType("mail_drafts")

    def _file_in_outbox(*a, **k):
        calls["filed"] = True
    md._file_in_outbox = _file_in_outbox
    md.init = lambda *a, **k: calls.setdefault("init", (a, k))

    def create_email_draft(account, to, subject, body, thread_id=""):
        calls["draft"] = (account, to, subject)
        md._file_in_outbox(account, to, subject, body, draft_id)   # what the real one does
        return (f"Draft saved to the studio Gmail Drafts folder (draft id {draft_id}), addressed to {to}."
                if draft_id else f"Draft saved to the studio Gmail Drafts folder, addressed to {to}.")
    md.create_email_draft = create_email_draft
    comp = types.ModuleType("composio")
    comp.Composio = lambda api_key="": object()
    monkeypatch.setitem(sys.modules, "mail_drafts", md)
    monkeypatch.setitem(sys.modules, "composio", comp)
    monkeypatch.setenv("COMPOSIO_API_KEY", "x")
    monkeypatch.setenv("STUDIO_GMAIL_ENTITY", "studio-entity")
    return calls


def test_create_studio_draft_never_files_an_outbox_row(monkeypatch):
    calls = _fake_gmail(monkeypatch)
    draft_id, msg = sq.create_studio_draft("ankit@myobvi.com", "s", GOOD_BODY_PROSE)
    assert draft_id == "r123"
    assert calls["draft"] == ("studio", "ankit@myobvi.com", "s")
    assert "filed" not in calls, "a first touch must reach the outbox only via the server's release"


def test_add_refuses_when_gmail_returns_no_draft_id(monkeypatch, tmp_path):
    _fake_gmail(monkeypatch, draft_id="")
    rows = [_row()]
    saved = {}
    monkeypatch.setattr(sq, "tracker_rows", lambda: (rows, list(rows[0].keys())))
    monkeypatch.setattr(sq, "load_queue", lambda: ({}, []))
    monkeypatch.setattr(sq, "save_queue", lambda q, queue: saved.setdefault("queue", queue))
    monkeypatch.setattr(sq, "write_tracker", lambda *a, **k: "bak")
    body = tmp_path / "b.txt"
    body.write_text(GOOD_BODY_PROSE)
    rc = sq.main(["add", "--to", "ankit@myobvi.com", "--subject", "19 ads, one product",
                  "--body-file", str(body), "--ad-count", "19"])
    assert rc == 1 and "queue" not in saved


def test_add_queues_stamps_and_records(monkeypatch, tmp_path):
    _fake_gmail(monkeypatch, draft_id="r999")
    rows = [_row(notes="page=Obvi id=2431731276838642", status="qualified")]
    saved, written = {}, {}
    monkeypatch.setattr(sq, "tracker_rows", lambda: (rows, list(rows[0].keys())))
    monkeypatch.setattr(sq, "load_queue", lambda: ({"key": sq.QUEUE_KEY}, []))
    monkeypatch.setattr(sq, "save_queue", lambda q, queue: saved.setdefault("queue", queue))
    monkeypatch.setattr(sq, "write_tracker", lambda rws, fields, tag: written.setdefault("rows", rws) and "bak")
    monkeypatch.setattr(sq, "DRAFT_DOC_DIR", str(tmp_path))
    monkeypatch.setattr(sq, "today_local", lambda: TODAY)
    body = tmp_path / "b.txt"
    body.write_text(GOOD_BODY_PROSE)
    rc = sq.main(["add", "--to", "ankit@myobvi.com", "--subject", "19 ads, one product",
                  "--body-file", str(body), "--ad-count", "19", "--evidence", "all tub shots"])
    assert rc == 0
    entry = saved["queue"][0]
    assert entry["draft_id"] == "r999" and entry["to"] == "ankit@myobvi.com"
    assert entry["queued_by"] == "money-shift" and "released" not in entry
    assert rows[0]["notes"].startswith("adlib 19 active [read live 2026-09-16]")
    doc = tmp_path / "outreach-drafts-shift-2026-09-16.md"
    assert doc.exists() and "19 ads, one product" in doc.read_text() and "all tub shots" in doc.read_text()


def test_add_dry_run_touches_nothing(monkeypatch, tmp_path):
    calls = _fake_gmail(monkeypatch)
    rows = [_row()]
    monkeypatch.setattr(sq, "tracker_rows", lambda: (rows, list(rows[0].keys())))
    monkeypatch.setattr(sq, "load_queue", lambda: ({}, []))
    monkeypatch.setattr(sq, "save_queue", lambda q, queue: pytest.fail("dry run saved the queue"))
    monkeypatch.setattr(sq, "write_tracker", lambda *a, **k: pytest.fail("dry run wrote the tracker"))
    body = tmp_path / "b.txt"
    body.write_text(GOOD_BODY_PROSE)
    rc = sq.main(["add", "--to", "ankit@myobvi.com", "--subject", "s", "--body-file", str(body),
                  "--ad-count", "19", "--dry-run"])
    assert rc == 0 and "draft" not in calls


CREATOR_LIST = """# Creator lane — prospect list

## Qualified

### MISTERARTHER — the best fit found so far
- **Platform:** Twitch `twitch.tv/misterarther`
- **Email:** `contact@misterarther.com` — **UNVERIFIED.** This came from a search-result
  summary, not a page I read. Confirm from his Twitch About panel first.

### Guzu
- **Platform:** Twitch `twitch.tv/guzu`
- **Email:** `guzubusiness@hotmail.com` — read directly off his own Twitch About panel.

### Dishsoap
- **Email:** `dishsoap@evolved.gg` — on his own Twitch About panel text.
"""

GOOD_EVIDENCE = "the 2026-09-14 GTA stream, the bank chase where he loses the car at 1:12:30"


def test_creator_entry_reads_the_list():
    got = sq.creator_entry(CREATOR_LIST, "guzubusiness@hotmail.com")
    assert got == {"found": True, "unverified": False, "name": "Guzu"}

    # The UNVERIFIED marker is on the same line and the one after it; both must count, because
    # an address that came from a search summary is a guess with Alex's name on it.
    bad = sq.creator_entry(CREATOR_LIST, "contact@misterarther.com")
    assert bad["found"] and bad["unverified"] and bad["name"] == "MISTERARTHER"

    assert sq.creator_entry(CREATOR_LIST, "nobody@nowhere.com")["found"] is False
    assert sq.creator_entry(CREATOR_LIST, "")["found"] is False


def test_plan_creator_refuses_everything_that_must_not_go_out():
    ok = dict(list_text=CREATOR_LIST, queue=[], to="guzubusiness@hotmail.com", subject="s",
              body=GOOD_BODY_PROSE, evidence=GOOD_EVIDENCE, offer_approved=True)

    _, p = sq.plan_creator(**ok)
    assert p == []

    _, p = sq.plan_creator(**{**ok, "offer_approved": False})
    assert any("not approved" in x for x in p)

    _, p = sq.plan_creator(**{**ok, "to": "contact@misterarther.com"})
    assert any("UNVERIFIED" in x for x in p)

    _, p = sq.plan_creator(**{**ok, "to": "someone@elsewhere.com"})
    assert any("not in the creator prospect list" in x for x in p)

    _, p = sq.plan_creator(**{**ok, "evidence": "watched his stream"})
    assert any("--evidence" in x for x in p)

    _, p = sq.plan_creator(**{**ok, "queue": [{"to": "guzubusiness@hotmail.com"}]})
    assert any("already in the queue" in x for x in p)

    _, p = sq.plan_creator(**{**ok, "subject": ""})
    assert any("no subject" in x for x in p)

    # The body guards are the Splitframe ones, not a second copy that can drift away from them.
    _, p = sq.plan_creator(**{**ok, "body": "too short"})
    assert any("too short" in x for x in p)


def test_creator_state_counts_who_is_left():
    # Guzu and Dishsoap are verified; MISTERARTHER is not and never counts as available.
    assert sq.creator_state(CREATOR_LIST, []) == {"available": 2, "queued_pending": 0}

    queue = [{"to": "guzubusiness@hotmail.com", "lane": "creator", "released": ""}]
    assert sq.creator_state(CREATOR_LIST, queue) == {"available": 1, "queued_pending": 1}

    # A released entry is out of the queue's pending slice but still never re-queued.
    queue = [{"to": "guzubusiness@hotmail.com", "lane": "creator",
              "released": "2026-09-16T13:00:00-04:00"}]
    assert sq.creator_state(CREATOR_LIST, queue) == {"available": 1, "queued_pending": 0}

    # A Splitframe entry is not a creator entry.
    queue = [{"to": "eric@bigbarker.com", "released": ""}]
    assert sq.creator_state(CREATOR_LIST, queue)["queued_pending"] == 0


def test_close_variant_alternates_within_the_experiment_only():
    """Assignment balances the two arms against EACH OTHER, not against the eighteen emails sent
    before the split. Counting those would send every email to the offer arm until it caught up
    to eighteen — and the offer arm would then be running against this week's list while the
    question arm's record came from last week's, so the close would be confounded with who was
    written to."""
    pre = [{"to": f"{i}@x.com"} for i in range(18)]          # no variant: written before the split
    assert sq.next_close_variant(pre) == "question"

    assert sq.next_close_variant(pre + [{"to": "a@x.com", "close_variant": "question"}]) == "offer"
    assert sq.next_close_variant(pre + [{"to": "a@x.com", "close_variant": "question"},
                                        {"to": "b@x.com", "close_variant": "offer"}]) == "question"
    assert sq.next_close_variant([{"to": "a@x.com", "close_variant": "offer"},
                                  {"to": "b@x.com", "close_variant": "offer"}]) == "question"
    assert sq.next_close_variant([]) == "question"


def test_close_report_reads_outcomes_per_arm():
    rows = [_row(brand="Q1", email="q1@x.com", sent_date="2026-09-15"),
            _row(brand="Q2", email="q2@x.com", sent_date="2026-09-15", replied="2026-09-16"),
            _row(brand="O1", email="o1@x.com", sent_date="2026-09-17"),
            _row(brand="Never", email="n@x.com")]
    queue = [{"to": "q1@x.com"},                                    # pre-split, so question
             {"to": "q2@x.com", "close_variant": "question"},
             {"to": "o1@x.com", "close_variant": "offer"}]
    assert sq.close_report(rows, queue) == {"question": {"sent": 2, "replied": 1},
                                            "offer": {"sent": 1, "replied": 0}}

    # The tracker's own column wins over the queue, so a row still reads right once the queue
    # entry has aged out of it.
    rows[0]["close_variant"] = "offer"
    assert sq.close_report(rows, [])["offer"]["sent"] == 1


def test_creator_entry_verified_beats_a_nearby_warning():
    """"UNVERIFIED" contains "VERIFIED", and the note that clears an address usually explains
    what it used to say. Neither may be read as the other."""
    cleared = """### MISTERARTHER
- **Email:** `contact@misterarther.com` — **VERIFIED 2026-09-17.** Read off the About panel.
  (It was listed UNVERIFIED because the address had only come from a search result.)
"""
    assert sq.creator_entry(cleared, "contact@misterarther.com")["unverified"] is False

    # The warning still stands when it is the address's own line that carries it...
    warned = "- **Email:** `x@y.com` — **UNVERIFIED.** From a search summary.\n"
    assert sq.creator_entry(warned, "x@y.com")["unverified"] is True

    # ...or the line under it, where the operator usually writes the caveat.
    below = "- **Email:** `x@y.com`\n  This is UNVERIFIED — confirm it off their own page.\n"
    assert sq.creator_entry(below, "x@y.com")["unverified"] is True


def test_offer_close_must_name_a_photo_on_their_own_site():
    """The offer close makes a claim the fabrication guard cannot see, because it is about the
    future: "I'll build you X." The first batch promised Antler Farms a static built from imagery
    of their free-grazing herds; they publish product bottles and a logo, so that ad could only
    have been made by inventing a farm."""
    dom = "antlerfarms.com"
    good = "https://antlerfarms.com/cdn/shop/files/AFDAV_bottle900.jpg"
    assert sq.offer_image_problems(good, dom) == []
    assert sq.offer_image_problems("https://www.antlerfarms.com/x.png?v=2", dom) == []

    assert any("required" in p for p in sq.offer_image_problems("", dom))
    assert any("not an image URL" in p for p in
               sq.offer_image_problems("https://antlerfarms.com/pages/about", dom))
    assert any("never one found elsewhere" in p for p in
               sq.offer_image_problems("https://unsplash.com/deer-herd.jpg", dom))

    # A row with no domain recorded cannot be checked against one; that is not a reason to refuse.
    assert sq.offer_image_problems(good, "") == []


def test_plan_add_only_demands_the_photo_for_the_offer_arm():
    rows = [_row(domain="myobvi.com")]
    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 19, "", "question", "")
    assert p == []

    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 19, "", "offer", "")
    assert any("--offer-image is required" in x for x in p)

    _, p = sq.plan_add(rows, [], "ankit@myobvi.com", "s", GOOD_BODY_PROSE, 19, "", "offer",
                       "https://myobvi.com/cdn/shop/files/tub.jpg")
    assert p == []


# ---------------------------------------------------------------------------
# Greeting policy. All 23 first touches ever sent went to a named founder; every
# in-band brand left carries only a front-desk address, so the next 150 emails
# were all going to open cold. A name found on the brand's OWN about page fixes
# that — but only a found one. An invented name is worse than no greeting.
# ---------------------------------------------------------------------------

def test_a_found_name_is_usable_at_a_front_desk():
    assert sq.greeting_for("shared", "Gillian", "hello@x.com") == "Gillian"
    assert sq.greeting_for("shared", "Amanda Chantal Bacon", "press@x.com") == "Amanda"
    assert sq.greeting_for("person", "Dana Reed", "dana@x.com") == "Dana"


def test_a_greeting_is_never_invented():
    assert sq.greeting_for("shared", "", "hello@x.com") == ""
    assert sq.greeting_for("shared", "   ", "info@x.com") == ""
    # A ticket desk is not written to at all, named or not.
    assert sq.greeting_for("ticket", "Dana Reed", "support@x.com") == ""
    assert sq.greeting_for("", "Dana Reed", "dana@x.com") == ""


def test_the_address_outranks_the_tracker_when_they_name_different_people():
    """Beauty From Bees publishes "A note from our Founder, Michelle" and answers at gillian@.
    Greeting Gillian's inbox as Michelle is worse than not greeting her — it is exactly what a
    mail merge looks like. Whoever the address names is who opens it."""
    assert sq.greeting_for("person", "Michelle", "gillian@beautyfrombees.ca") == "Gillian"
    # They agree: the tracker's spelling wins, because it carries the real capitalisation.
    assert sq.greeting_for("person", "Maxx Appelman", "maxx.appelman@x.com") == "Maxx"
    # No tracker name at all, but the address is plainly a person's.
    assert sq.greeting_for("person", "", "gillian@x.com") == "Gillian"


def test_name_in_address_ignores_role_inboxes():
    for addr in ("hello@x.com", "info@x.com", "support@x.com", "oudwarellc@x.com",
                 "justdoughit@x.com", "goodday@x.com"):
        assert sq.name_in_address(addr) == "", addr
    assert sq.name_in_address("jennifer@x.com") == "Jennifer"
    assert sq.name_in_address("brittany.stone@x.com") == "Brittany"


def test_named_front_desks_are_drafted_before_anonymous_ones():
    rows = [
        _row(brand="Anon", email="", email_generic="hello@anon.com", contact_name="",
             notes="adlib 20 active [read live 2026-09-17]"),
        _row(brand="Named", email="", email_generic="hello@named.com", contact_name="Gillian Ray",
             notes="adlib 20 active [read live 2026-09-17]"),
    ]
    order = [t["brand"] for t in sq.next_targets(rows, [])]
    assert order.index("Named") < order.index("Anon")
    named = [t for t in sq.next_targets(rows, []) if t["brand"] == "Named"][0]
    assert named["greet"] == "Gillian"


# ---------------------------------------------------------------------------
# revise: fixing a queued first touch in place. The email lives in two stores —
# the Gmail draft the sender will send, and the queue entry that is the record.
# Editing one by hand drifts them apart, and the sender sends the draft. One
# command, both stores, the same guards as `add`.
# ---------------------------------------------------------------------------

def _queued(**kw):
    e = {"brand": "Acme", "to": "hello@acme.com", "subject": "s", "body": "b",
         "draft_id": "r1", "close_variant": "question", "released": None}
    e.update(kw)
    return e


def test_revise_refuses_a_released_entry():
    """Once released it is in the outbox on a 3 h timer or already sent. Editing then either
    races the sender or silently differs from what actually went out."""
    q = [_queued(released="2026-09-15T07:30:00-04:00")]
    _e, problems = sq.plan_revise(q, "hello@acme.com", None, None, None, "acme.com")
    assert any("already released" in p for p in problems)


def test_revise_refuses_an_address_not_in_the_queue():
    _e, problems = sq.plan_revise([], "nobody@nowhere.com", None, None, None, "")
    assert any("not in the first-touch queue" in p for p in problems)


def test_revise_refuses_an_entry_with_no_draft_to_rewrite():
    _e, problems = sq.plan_revise([_queued(draft_id="")], "hello@acme.com",
                                  None, None, None, "acme.com")
    assert any("no draft id" in p for p in problems)


def test_revise_applies_the_same_body_guards_as_add():
    """A fix must not be a way around the guards that stop a bad email going out."""
    _e, problems = sq.plan_revise([_queued()], "hello@acme.com", "subj", "too short", None,
                                  "acme.com")
    assert problems, "a body that add would reject must be rejected here too"


def test_revise_keeps_the_offer_arm_honest():
    """An offer email promises a static built from a named photo. Swapping the body without
    a valid photo on the brand's own domain is the Antler Farms failure again."""
    q = [_queued(close_variant="offer", offer_image="https://acme.com/a.jpg")]
    _e, problems = sq.plan_revise(q, "hello@acme.com", None, None,
                                  "https://someone-else.com/b.jpg", "acme.com")
    assert problems, "an offer image off the brand's own domain must be refused"


def test_revise_leaves_fields_alone_when_not_given():
    """Omitting --subject/--body-file keeps what is there; only what was passed changes."""
    q = [_queued(close_variant="offer", offer_image="https://acme.com/a.jpg")]
    entry, problems = sq.plan_revise(q, "hello@acme.com", None, None, None, "acme.com")
    assert problems == [] and entry["subject"] == "s" and entry["body"] == "b"


# ---------------------------------------------------------------------------
# Hunter targets. The free tier is 25 searches a CYCLE, so every wasted one is a
# brand that stays nameless for a month. This returned 81 targets led by Obvi,
# Maxbone, Moon Juice and Brightland — all already emailed or already queued.
# ---------------------------------------------------------------------------

def _hrow(**kw):
    base = {"brand": "Acme", "domain": "acme.com", "status": "qualified", "email": "",
            "email_generic": "", "contact_name": "", "sent_date": "", "replied": "",
            "outcome": "", "email_checked": "", "notes": "adlib 10 active [read live 2026-09-17]"}
    base.update(kw)
    return base


def test_a_brand_already_emailed_is_not_worth_a_search():
    rows = [_hrow(brand="Sent", sent_date="2026-09-17"),
            _hrow(brand="Replied", replied="2026-09-18"),
            _hrow(brand="Closed", outcome="no_response"),
            _hrow(brand="Fresh")]
    assert [t["brand"] for t in sq.hunter_targets(rows)] == ["Fresh"]


def test_a_brand_queued_to_send_is_not_worth_a_search():
    rows = [_hrow(brand="Queued", email_generic="hello@acme.com"), _hrow(brand="Fresh")]
    queue = [{"to": "hello@acme.com"}]
    assert [t["brand"] for t in sq.hunter_targets(rows, queue)] == ["Fresh"]


def test_a_row_that_already_names_a_human_does_not_need_hunter():
    """The old test was is_person(email), which only knows a fixed list of first names — so
    Obvi's ankit@myobvi.com read as 'needs Hunter' despite plainly being a person."""
    rows = [_hrow(brand="Obvi", email="ankit@myobvi.com", contact_name="Ankit Patel"),
            _hrow(brand="Fresh")]
    assert [t["brand"] for t in sq.hunter_targets(rows)] == ["Fresh"]


def test_a_search_already_spent_this_cycle_is_not_repeated():
    rows = [_hrow(brand="Done", email_checked="2026-09-25"), _hrow(brand="Fresh")]
    assert [t["brand"] for t in sq.hunter_targets(rows, (), "2026-09-24")] == ["Fresh"]


def test_hunter_does_not_re_answer_a_question_it_already_answered():
    """ZitSticka: Hunter ran and the only address it could offer was support@. target_address
    returns nothing for a ticket desk, so the row looks unreachable forever — asking again
    next cycle spends a search on a settled question."""
    rows = [_hrow(brand="ZitSticka", email="support@zitsticka.com",
                  email_checked="2026-09-10"), _hrow(brand="Fresh")]
    assert [t["brand"] for t in sq.hunter_targets(rows)] == ["Fresh"]
    # but a ticket desk Hunter has NEVER been asked about is still worth a search
    rows2 = [_hrow(brand="Untried", email="support@untried.com")]
    assert [t["brand"] for t in sq.hunter_targets(rows2)] == ["Untried"]


def test_searches_go_to_the_smallest_nameless_in_band_brands_first():
    """25 searches against 58 candidates makes the ORDER the decision. A brand running eight
    ads is a few people whose founder reads their own mail; one that already has a name from
    its about page can at least be greeted today, so it needs the search less."""
    rows = [
        _hrow(brand="TooBig", notes="adlib 200 active [read live 2026-09-17]"),
        _hrow(brand="Borderline", notes="adlib 90 active [read live 2026-09-17]"),
        _hrow(brand="Named", email_generic="hello@named.com", contact_name="Gillian Ray",
              notes="adlib 9 active [read live 2026-09-17]"),
        _hrow(brand="Small", notes="adlib 8 active [read live 2026-09-17]"),
        _hrow(brand="Mid", notes="adlib 30 active [read live 2026-09-17]"),
    ]
    got = [t["brand"] for t in sq.hunter_targets(rows)]
    assert got[0] == "Small"
    assert got.index("Mid") < got.index("Named"), "a nameless brand needs it more"
    # 100+ active ads is an in-house creative team: excluded outright, not merely ranked last.
    assert "TooBig" not in got
    assert got[-1] == "Borderline", "the 51-100 band is a last resort, not a first pick"
