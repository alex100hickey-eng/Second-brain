"""Tests for the Splitframe funnel report (scripts/funnel_report.py). No network.

Runs under run_tests.py for real: the __main__ block hands the file to pytest. Without that
block, run_tests would run this file as a script, define the tests, and exit 0 without running
any of them. That's what happens to every pytest-style file here that lacks one.
"""
import ast
import csv
import importlib.util
import os
import re
import sys
from datetime import date

import pytest

# Relative to this file, NOT ~/second-brain: from a git worktree the home-dir path tests the
# main tree's code instead of the code under review.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
SOURCE = os.path.join(SCRIPTS, "funnel_report.py")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = _load("funnel_report")
sfd = _load("splitframe_daily")

TODAY = date(2026, 9, 20)
FIELDS = ["brand", "domain", "category", "email", "email_generic", "status", "wave",
          "sent_date", "followup1_date", "followup2_date", "replied", "call_date", "outcome",
          "notes", "email_status", "contact_name", "close_variant"]


def _row(**kw):
    base = {f: "" for f in FIELDS}
    base.update({"brand": "Acme", "category": "snacks", "email": "dana@acme.com",
                 "contact_name": "Dana Reed", "status": "qualified", "sent_date": "2026-09-10",
                 "followup1_date": "2026-09-13", "followup2_date": "2026-09-17"})
    base.update(kw)
    return base


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


# ---- the guardrails: read-only on the tracker, no send path ----

def test_the_module_has_no_way_to_write_the_tracker():
    """The report may read the tracker and nothing more. The tracker is the one file every
    lane depends on, and the Mac is its only writer."""
    src = open(SOURCE, encoding="utf-8").read()
    for forbidden in ("csv.writer", "DictWriter", "shutil", "TRACKER, \"w\"", "TRACKER, 'w'"):
        assert forbidden not in src, f"funnel_report grew a tracker write: {forbidden}"


def test_write_report_refuses_the_tracker_and_non_markdown(tmp_path, monkeypatch):
    tracker = tmp_path / "prospect-tracker.csv"
    _write_csv(tracker, [_row()])
    monkeypatch.setattr(fr, "TRACKER", str(tracker))
    before = tracker.read_bytes()
    with pytest.raises(ValueError):
        fr.write_report("x", str(tracker))
    with pytest.raises(ValueError):
        fr.write_report("x", str(tmp_path / "other.csv"))
    assert tracker.read_bytes() == before


def test_no_send_path_and_no_sender_import():
    src = open(SOURCE, encoding="utf-8").read()
    for marker in ("GMAIL_SEND", "GMAIL_REPLY_TO_THREAD", "SEND_DRAFT", "smtplib", "sendmail",
                   "composio", "Composio"):
        assert marker not in src, f"funnel_report grew a send path: {marker}"
    for node in ast.walk(ast.parse(src)):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        assert not any("splitframe_send" in n for n in names)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "spec_from_file_location":
            assert not any(isinstance(a, ast.Constant) and "splitframe_send" in str(a.value)
                           for a in node.args)


def test_main_writes_only_the_report_and_leaves_the_tracker_byte_identical(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Money").mkdir(parents=True)
    tracker = vault / "Money" / "prospect-tracker.csv"
    _write_csv(tracker, [_row(), _row(brand="Beta", email="", email_generic="hello@beta.com",
                                      contact_name="", close_variant="offer")])
    log = tmp_path / "splitframe_send.log"
    log.write_text("2026-09-10 08:00 item 1: SENT to dana@acme.com (draft r1)\n")
    monkeypatch.setattr(fr, "VAULT", str(vault))
    monkeypatch.setattr(fr, "TRACKER", str(tracker))
    monkeypatch.setattr(fr, "SEND_LOG", str(log))
    before, mtime = tracker.read_bytes(), tracker.stat().st_mtime_ns
    assert fr.main([]) == 0
    assert tracker.read_bytes() == before and tracker.stat().st_mtime_ns == mtime
    written = sorted(p.name for p in (vault / "Money").iterdir())
    assert written[0].startswith("Funnel — ") and written[0].endswith(".md")
    assert written[1:] == ["prospect-tracker.csv"]           # and no .tmp left behind
    assert "# Funnel — " in (vault / "Money" / written[0]).read_text()


# ---- the slices ----

def test_every_send_lands_in_exactly_one_close_inbox_and_wave():
    rows = [
        _row(brand="A", close_variant="offer", wave="1"),
        _row(brand="B", close_variant="question", email="", email_generic="hello@b.com",
             contact_name="", replied="2026-09-15"),
        _row(brand="C", close_variant="", wave="3", call_date="2026-09-18", replied="2026-09-14"),
        _row(brand="D", email="support@d.com", contact_name=""),
        _row(brand="E", sent_date=""),                                 # never sent: not counted
        _row(brand="Guzu", category="creator", close_variant="offer",
             email="", email_generic="guzu@hotmail.com", contact_name=""),
    ]
    rep = fr.build(rows, TODAY)
    arms = dict(rep["by_arm"])
    assert arms["offer"]["sent"] == 1                     # the creator's offer is a different pitch
    assert arms["question"]["sent"] == 1 and arms["question"]["replied"] == 1
    assert arms["pre-split"]["sent"] == 2 and arms["pre-split"]["calls"] == 1
    tiers = dict(rep["by_tier"])
    assert tiers["person"]["sent"] == 2 and tiers["shared"]["sent"] == 1
    assert tiers["ticket"]["sent"] == 1
    waves = dict(rep["by_wave"])
    assert waves["1"]["sent"] == 1 and waves["3"]["sent"] == 1 and waves["none"]["sent"] == 2
    lanes = dict(rep["by_lane"])
    assert lanes["splitframe"]["sent"] == 4 and lanes["creator"]["sent"] == 1
    for groups in (rep["by_arm"], rep["by_tier"], rep["by_wave"]):
        assert sum(s["sent"] for _, s in groups) == rep["splitframe"]["sent"] == 4


def test_zero_rows_still_show_for_support_ticket_and_both_arms():
    """"0 sent to support tickets" is information. A missing row reads like it was forgotten."""
    rep = fr.build([_row(close_variant="")], TODAY)
    assert [k for k, _ in rep["by_tier"]][:3] == ["person", "shared", "ticket"]
    assert {"offer", "question"} <= {k for k, _ in rep["by_arm"]}
    text = fr.render(rep)
    assert "| support ticket | 0 |" in text and "| offer | 0 |" in text


def test_an_unrecognised_close_gets_its_own_row_not_folded_in():
    rep = fr.build([_row(close_variant="Offer "), _row(brand="B", close_variant="offr")], TODAY)
    arms = dict(rep["by_arm"])
    assert arms["offer"]["sent"] == 1 and arms["offr"]["sent"] == 1


def test_tier_matches_the_drafter():
    """A reply has to be credited to the inbox the drafter actually wrote to."""
    for row in (_row(), _row(email="", email_generic="hello@acme.com", contact_name=""),
                _row(email="gracie@acme.com", contact_name="")):
        _addr, t = sfd.target_address(row)
        assert fr.tier(row) == t


# ---- follow-ups: due vs done ----

def test_followups_against_the_send_log():
    sends = fr.parse_send_log(
        "2026-09-10 08:00 item 1: SENT to dana@acme.com (draft r1)\n"      # first touch
        "2026-09-14 08:00 item 2: SENT to dana@acme.com (draft r2) — automatically\n")
    states = fr.followup_states(_row(), TODAY, sends=sends)
    assert states == [(2, date(2026, 9, 13), "sent"), (3, date(2026, 9, 17), "overdue")]
    assert fr.followup_states(_row(), date(2026, 9, 17), sends=sends)[1][2] == "due today"
    assert fr.followup_states(_row(), date(2026, 9, 16), sends=sends)[1][2] == "upcoming"


def test_a_first_touch_logged_a_day_late_is_not_a_followup():
    sends = fr.parse_send_log("2026-09-11 00:30 item 1: SENT to dana@acme.com (draft r1)\n")
    assert [s for _, _, s in fr.followup_states(_row(), TODAY, sends=sends)] == \
        ["overdue", "overdue"]


def test_a_followup_to_the_front_desk_column_counts():
    """email_generic has been missed by five consumers already (send gate, stamper, cap, reply
    watch). The follow-up count reads both columns."""
    row = _row(email="", email_generic="hello@acme.com", contact_name="")
    sends = fr.parse_send_log("2026-09-13 09:00 item 7: SENT to hello@acme.com (draft r7)\n")
    assert fr.followup_states(row, TODAY, sends=sends)[0][2] == "sent"


def test_no_send_log_means_unknown_never_zero_and_never_overdue():
    assert fr.read_send_log("/nonexistent-dir-for-tests/splitframe_send.log") is None
    rep = fr.build([_row()], TODAY, sends=None)
    assert rep["fu_source"] is None and not rep["followups"]["overdue"]
    assert len(rep["followups"]["due (unverified)"]) == 2
    text = fr.render(rep)
    assert "unknown" in text.lower() and "Overdue" not in text
    assert "follow-up status unknown" in fr.summary_line(rep)
    assert "?/2" in text


def test_on_the_server_drafted_counts_as_done():
    rep = fr.build([_row()], TODAY, drafted={"dana@acme.com": [2]})
    assert rep["fu_source"] == "drafted state"
    assert rep["followups"]["drafted"] == 1
    assert [x["touch"] for x in rep["followups"]["overdue"]] == [3]
    assert "never drafted" in fr.summary_line(rep)


def test_a_brand_that_replied_is_owed_nothing_and_shows_what_it_answered():
    sends = fr.parse_send_log("2026-09-13 09:00 item 2: SENT to dana@acme.com (draft r2)\n")
    row = _row(close_variant="offer", wave="2", replied="2026-09-14")
    assert [s for _, _, s in fr.followup_states(row, TODAY, sends=sends)] == ["sent", "stopped"]
    rep = fr.build([row], TODAY, sends=sends)
    assert not rep["followups"]["overdue"]
    (resp,) = rep["responses"]
    assert (resp["arm"], resp["tier"], resp["wave"], resp["after_touch"]) == \
        ("offer", "person", "2", 2)
    assert "| Acme | splitframe | offer | named contact | 2 |" in fr.render(rep)


def test_sends_with_no_tracker_row_are_named():
    """No tracker row means no follow-up clock. That's how creator prospects got one email."""
    sends = fr.parse_send_log("2026-09-19 09:00 item 9: SENT to zerbs@evolved.gg (draft r9)\n"
                              "2026-09-19 09:00 item 8: SENT to dana@acme.com (draft r8)\n")
    rep = fr.build([_row()], TODAY, sends=sends)
    assert [x["to"] for x in rep["untracked"]] == ["zerbs@evolved.gg"]
    assert "zerbs@evolved.gg" in fr.render(rep)


def test_a_correction_counts_a_send_the_log_missed(tmp_path):
    """Antler Farms' FU1 left at 01:56 on 09-23 and never reached the log. Without the
    correction, its FU2 would later be counted as FU1 and the brand would read one touch behind."""
    log = tmp_path / "send.log"
    log.write_text("2026-09-10 08:00 item 1: SENT to dana@acme.com (draft r1)\n")
    fix = tmp_path / "corrections.log"
    assert fr.followup_states(_row(), TODAY, sends=fr.read_send_log(str(log), str(fix)))[0][2] \
        == "overdue"
    fix.write_text("2026-09-13 01:56 item 7: SENT to dana@acme.com (draft r7) — recorded late\n")
    assert fr.followup_states(_row(), TODAY, sends=fr.read_send_log(str(log), str(fix)))[0][2] \
        == "sent"
    assert fr.read_send_log(str(tmp_path / "missing.log"), str(fix)) is None, \
        "a correction alone is not a send log"


def test_the_send_log_parser_only_counts_real_sends():
    sends = fr.parse_send_log(
        "2026-09-22 19:57 daily cap reached (10/10) — auto-sends deferred to tomorrow\n"
        "2026-09-19 09:00 item 3: recipient 'x@y.com' is not an approved address — HELD, not sent\n"
        "2026-09-19 09:01 item 4: SEND FAILED to a@b.com — boom\n"
        "2026-09-19 09:02 item 5: SENT to A@B.com (draft r5) — automatically\n")
    assert sends == {"a@b.com": [date(2026, 9, 19)]}


def test_the_report_says_nothing_about_ai():
    rep = fr.build([_row()], TODAY, sends={})
    assert not re.search(r"\bAI\b", fr.render(rep) + fr.summary_line(rep))


# ---- the hook in the daily job ----

def test_the_daily_job_logs_the_funnel_and_the_hook_cannot_kill_it():
    tree = ast.parse(open(os.path.join(SCRIPTS, "splitframe_daily.py"), encoding="utf-8").read())
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "funnel_headline"
               for c in ast.walk(main)), "splitframe_daily.main() no longer reports the funnel"
    line = sfd.funnel_headline([_row()], {"dana@acme.com": [2, 3]})
    assert line.startswith("funnel: 1 sent") and "0 follow-up(s) never drafted" in line
    assert sfd.funnel_headline(None, None).startswith("funnel: unavailable")


def test_the_daily_job_still_has_no_send_path():
    src = open(os.path.join(SCRIPTS, "splitframe_daily.py"), encoding="utf-8").read()
    for forbidden in ("GMAIL_SEND_EMAIL", "GMAIL_SEND", "send_email", "smtplib", "SEND_DRAFT"):
        assert forbidden not in src


# ---- keeping itself current ----

def test_refresh_rewrites_only_when_the_content_changed(tmp_path, monkeypatch):
    """reply_watch calls this every 30 minutes. A rewrite for a moved timestamp would be a vault
    commit every half hour with nothing in it."""
    vault = tmp_path / "vault"
    (vault / "Money").mkdir(parents=True)
    tracker = vault / "Money" / "prospect-tracker.csv"
    _write_csv(tracker, [_row()])
    log = tmp_path / "send.log"
    log.write_text("2026-09-10 08:00 item 1: SENT to dana@acme.com (draft r1)\n")
    monkeypatch.setattr(fr, "VAULT", str(vault))
    monkeypatch.setattr(fr, "TRACKER", str(tracker))
    monkeypatch.setattr(fr, "SEND_LOG", str(log))
    path = fr.refresh(TODAY)
    assert path and os.path.exists(path)
    assert fr.refresh(TODAY) is None, "same data, no rewrite"
    _write_csv(tracker, [_row(replied="2026-09-19")])
    assert fr.refresh(TODAY) == path, "a reply landed, so the report changes"
    assert "| Acme |" in open(path, encoding="utf-8").read()


def test_the_reply_watcher_refreshes_the_report_and_cannot_be_killed_by_it(tmp_path, monkeypatch):
    rw = _load("reply_watch")
    tree = ast.parse(open(os.path.join(SCRIPTS, "reply_watch.py"), encoding="utf-8").read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_refresh_funnel"
               for c in ast.walk(main))
    monkeypatch.setattr(rw, "LOG", str(tmp_path / "watch.log"))

    def boom(*a, **k):
        raise RuntimeError("funnel report broke")
    monkeypatch.setattr(importlib.util, "spec_from_file_location", boom)
    rw._refresh_funnel()                     # must not raise
    assert "funnel report not refreshed" in (tmp_path / "watch.log").read_text()


# ---- the contract against the real tracker, when this machine has it ----

def test_the_real_tracker_reads_cleanly_and_every_send_is_sliced():
    """Catches schema drift, like a renamed column or a new close value, on the real file.
    Read-only."""
    if not os.path.exists(fr.TRACKER):
        pytest.skip("no tracker on this machine")
    before = os.stat(fr.TRACKER).st_mtime_ns
    rows = fr.tracker_rows()
    for col in ("close_variant", "sent_date", "followup1_date", "followup2_date", "replied",
                "call_date", "outcome", "wave", "email", "email_generic", "contact_name"):
        assert col in rows[0], f"tracker lost the {col} column"
    rep = fr.build(rows, TODAY)
    for groups in (rep["by_arm"], rep["by_tier"], rep["by_wave"]):
        assert sum(s["sent"] for _, s in groups) == rep["splitframe"]["sent"]
    assert os.stat(fr.TRACKER).st_mtime_ns == before

# ---- an evicted tracker (iCloud "dataless") ----

def test_an_evicted_tracker_is_read_from_the_git_mirror_and_the_report_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "TRACKER", str(tmp_path / "evicted.csv"))
    monkeypatch.setattr(fr, "_mirror_text", lambda: "brand,domain,sent_date\nMoon Juice,moonjuice.com,2026-09-19\n")
    rows = fr.tracker_rows()
    assert rows[0]["brand"] == "Moon Juice"
    assert "vault git mirror" in fr.MIRROR_NOTE
    text = fr.render(fr.build(rows, date(2026, 9, 24), sends=None))
    assert "Built from the vault git mirror" in text


def test_a_readable_tracker_carries_no_mirror_note(tmp_path, monkeypatch):
    t = tmp_path / "t.csv"
    t.write_text("brand,domain,sent_date\nA,a.com,\n", encoding="utf-8")
    monkeypatch.setattr(fr, "TRACKER", str(t))
    fr.tracker_rows()
    assert fr.MIRROR_NOTE == ""


def test_an_explicit_path_and_a_missing_mirror_both_still_fail_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "_mirror_text", lambda: "brand\nX\n")
    with pytest.raises(OSError):
        fr.tracker_rows(str(tmp_path / "gone.csv"))
    monkeypatch.setattr(fr, "TRACKER", str(tmp_path / "evicted.csv"))
    monkeypatch.setattr(fr, "_mirror_text", lambda: None)
    with pytest.raises(OSError):
        fr.tracker_rows()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
