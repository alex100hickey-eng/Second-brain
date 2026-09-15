"""Tests for the Splitframe daily operator (scripts/splitframe_daily.py). No network."""
import ast
import csv
import importlib.util
import os
from datetime import date

import pytest

SPEC = importlib.util.spec_from_file_location(
    "splitframe_daily", os.path.expanduser("~/second-brain/scripts/splitframe_daily.py"))
sfd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sfd)


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
    src = open(os.path.expanduser("~/second-brain/scripts/splitframe_daily.py")).read()
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
    assert sfd.parse_body('{"body": ""}') == '{"body": ""}'      # empty body is not a body


# ---- the send path: where it is allowed to live, and where it must never appear ----

SEND_MARKERS = ("GMAIL_SEND_DRAFT", "GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD",
                "smtplib", "sendmail")
SENDER = os.path.expanduser("~/second-brain/scripts/splitframe_send.py")


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
    src = open(os.path.expanduser("~/second-brain/scripts/splitframe_daily.py")).read()
    tree = ast.parse(src)
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
    assert any(isinstance(c.func, ast.Attribute) and c.func.attr == "init"
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "outbox"
               for c in calls), "splitframe_daily.main() never calls outbox.init()"
