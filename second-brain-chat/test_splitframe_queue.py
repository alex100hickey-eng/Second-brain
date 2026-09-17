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
    rows = [
        _row(),                                                              # in band
        _row(brand="Emi Jay", email="julianne@emijay.com", notes="adlib 84 active; id=4542"),   # out
        _row(brand="Fresh", email="x@fresh.com", notes="page=Fresh id=1234567"),                # unknown
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
    assert [t["brand"] for t in got] == ["Obvi", "Fresh", "Emi Jay"]
    assert got[0]["page_id"] == "2431731276838642"
    assert got[0]["band"] == "in" and got[1]["band"] == "unknown" and got[2]["band"] == "out"


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
    assert any("shared inbox" in x for x in p)
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


def test_status_report_runway_math():
    rows = [_row()]
    queue = [{"to": "a@x.com", "brand": "A", "subject": "s", "released": ""},
             {"to": "b@x.com", "brand": "B", "subject": "s", "released": "2026-09-16T07:30:00-04:00"},
             {"to": "c@x.com", "brand": "C", "subject": "s", "released": "2026-09-15T07:30:00-04:00"}]
    rep = sq.status_report(rows, queue, TODAY)
    assert rep["pending_count"] == 1 and rep["released_today"] == 1
    assert rep["runway_days"] == 0.2 and rep["need_drafts"] == 9
    assert [t["brand"] for t in rep["next_targets"]] == ["Obvi"]


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
