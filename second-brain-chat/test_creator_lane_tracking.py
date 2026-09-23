"""Creator-lane prospects must exist in the tracker, and a reply must be credited to the right one.

2026-09-22: Dishsoap, Zerbs, masondota2 and Sequisha were each sent a $400/mo first touch with no
tracker row. The follow-up clock only starts on an existing row, and the reply watcher, bounce
watch and funnel report all read the tracker, so each got one email and nothing could follow it
up, see a reply to it or count its bounce (masondota2 bounced unseen).

Once they're tracked, a second trap opens. Dishsoap and Zerbs share a talent-agency domain, and
the reply watcher kept one brand per domain, so a reply from Dishsoap would have stamped Zerbs.

Every file the code touches here is a temp file. No test writes the real tracker.
"""
import csv
import importlib.util
import os
import sys
import types
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


q = _load("splitframe_queue", "splitframe_queue_under_test")
rw = _load("reply_watch", "reply_watch_under_test")
sfd = _load("splitframe_daily", "splitframe_daily_under_test")

FIELDS = ["brand", "domain", "category", "email", "email_generic", "status", "wave", "sent_date",
          "followup1_date", "followup2_date", "replied", "call_date", "outcome", "notes",
          "contact_name", "close_variant", "adlib_url"]

LIST = """# Creator Lane — Prospects

### Dishsoap
- **Platform:** Twitch `twitch.tv/dishsoap` (Valorant)
- **Email:** `dishsoap@evolved.gg` — on his own Twitch About panel text.

### Zerbs
- **Platform:** Twitch `twitch.tv/zerbs` (Dead by Daylight)
- **Email:** `Zerbs@evolved.gg` — own Twitch About panel.

### Sequisha
- **Platform:** Twitch `twitch.tv/sequisha`
- **Email:** `sequishalive@gmail.com` — re-verified live 2026-09-19.
"""

LOG = ("2026-09-22 00:57 item 19623: SENT to zerbs@evolved.gg (draft r1) — automatically\n"
       "2026-09-22 00:57 item 19622: SENT to dishsoap@evolved.gg (draft r2) — automatically\n"
       "2026-09-22 00:57 item 19628: SENT to sequishalive@gmail.com (draft r3) — automatically\n"
       "2026-09-22 00:56 item 19629: SENT to info@nativepet.com (draft r4) — automatically\n"
       "2026-09-25 10:00 item 19700: SENT to zerbs@evolved.gg (draft r5) — automatically\n")


def _dtc(**kw):
    row = {f: "" for f in FIELDS}
    row.update({"brand": "Native Pet", "category": "pet", "email_generic": "info@nativepet.com",
                "status": "qualified", "domain": "nativepet.com"})
    row.update(kw)
    return row


# ---- the row itself ----

def test_a_creator_row_has_the_same_shape_as_the_hand_made_guzu_row():
    row = q.creator_tracker_row(LIST, "Zerbs@evolved.gg", "Zerbs", "2026-09-23", FIELDS,
                                sent_date="2026-09-22")
    assert set(row) == set(FIELDS)
    assert (row["brand"], row["domain"], row["category"], row["email"], row["status"],
            row["close_variant"]) == ("Zerbs", "twitch.tv/zerbs", "creator", "zerbs@evolved.gg",
                                      "qualified", "offer")
    assert (row["followup1_date"], row["followup2_date"]) == ("2026-09-25", "2026-09-29")
    assert "NOT ad creative" in row["notes"] and "AI" not in row["notes"].split()


def test_the_backfill_finds_exactly_the_creators_emailed_without_a_row():
    rows = [_dtc()]
    found = q.untracked_creator_sends(LIST, rows, LOG)
    assert found == [("dishsoap@evolved.gg", "2026-09-22"), ("sequishalive@gmail.com", "2026-09-22"),
                     ("zerbs@evolved.gg", "2026-09-22")], "first send date, not a later follow-up"
    rows.append(q.creator_tracker_row(LIST, "zerbs@evolved.gg", "Zerbs", "x", FIELDS))
    assert "zerbs@evolved.gg" not in dict(q.untracked_creator_sends(LIST, rows, LOG))


def test_a_backfilled_creator_gets_its_follow_ups_and_a_bounced_one_does_not():
    ok = q.creator_tracker_row(LIST, "dishsoap@evolved.gg", "Dishsoap", "x", FIELDS,
                               sent_date="2026-09-22")
    dead = dict(ok, brand="masondota2", email="masondota2@afkcreators.com",
                outcome="bounced (recorded 2026-09-23)")
    due = sfd.due_followups([ok, dead], date(2026, 9, 25), {})
    assert [(r["brand"], t) for r, t in due] == [("Dishsoap", 2)]
    assert sfd.voice_for(ok) is sfd.CREATOR_VOICE, "follow-ups pitch clips, not ad creative"


# ---- the DTC side must never see a creator ----

def test_no_dtc_list_offers_a_creator_row():
    creator = q.creator_tracker_row(LIST, "dishsoap@evolved.gg", "Dishsoap", "x", FIELDS)
    rows = [creator, _dtc()]
    assert [t["brand"] for t in q.next_targets(rows, [])] == ["Native Pet"]
    assert all(r["brand"] != "Dishsoap" for r in q.hunter_targets(rows, []))


def test_the_creator_offer_is_not_counted_as_an_arm_of_the_close_ab():
    rows = [q.creator_tracker_row(LIST, "dishsoap@evolved.gg", "Dishsoap", "x", FIELDS,
                                  sent_date="2026-09-22"),
            _dtc(sent_date="2026-09-22", close_variant="offer")]
    assert q.close_report(rows, [])["offer"]["sent"] == 1


# ---- the commands, on temp files ----

@pytest.fixture
def vault(tmp_path, monkeypatch):
    tracker = tmp_path / "prospect-tracker.csv"
    with open(tracker, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(_dtc())
    (tmp_path / "list.md").write_text(LIST, encoding="utf-8")
    (tmp_path / "send.log").write_text(LOG, encoding="utf-8")
    monkeypatch.setattr(q, "TRACKER", str(tracker))
    monkeypatch.setattr(q, "CREATOR_PROSPECTS", str(tmp_path / "list.md"))
    monkeypatch.setattr(q, "SEND_LOG", str(tmp_path / "send.log"))

    def rows():
        with open(tracker, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return rows


def test_backfill_is_a_dry_run_unless_told_to_write(vault):
    before = vault()
    assert q.main(["creator-backfill"]) == 0
    assert vault() == before


def test_backfill_writes_rows_and_a_bounced_address_is_closed(vault):
    assert q.main(["creator-backfill", "--write", "--bounced", "sequishalive@gmail.com"]) == 0
    by = {r["email"]: r for r in vault()}
    assert by["zerbs@evolved.gg"]["sent_date"] == "2026-09-22"
    assert by["sequishalive@gmail.com"]["outcome"].startswith("bounced")
    assert not by["dishsoap@evolved.gg"]["outcome"]
    assert q.main(["creator-backfill", "--write"]) == 0
    assert len(vault()) == 4, "a second run adds nothing"


def test_queueing_a_creator_adds_the_tracker_row_once(vault, tmp_path, monkeypatch):
    body = tmp_path / "body.txt"
    body.write_text("hi", encoding="utf-8")
    saved = []
    monkeypatch.setattr(q, "load_queue", lambda: ({}, []))
    monkeypatch.setattr(q, "save_queue", lambda qq, queue: saved.append(list(queue)))
    monkeypatch.setattr(q, "plan_creator", lambda *a, **k: ({"name": "Dishsoap"}, []))
    monkeypatch.setattr(q, "create_studio_draft", lambda to, s, b: ("r-test", "ok"))
    monkeypatch.setattr(q, "record_creator_doc", lambda *a, **k: str(tmp_path / "doc.md"))
    args = ["creator", "--to", "dishsoap@evolved.gg", "--subject", "s", "--body-file", str(body),
            "--evidence", "watched it"]
    assert q.main(args) == 0
    rows = [r for r in vault() if r["email"] == "dishsoap@evolved.gg"]
    assert len(rows) == 1 and rows[0]["category"] == "creator" and not rows[0]["sent_date"]
    assert q.main(args) == 0
    assert len([r for r in vault() if r["email"] == "dishsoap@evolved.gg"]) == 1


# ---- the reply watcher credits the right creator ----

def _creator_rows():
    return [q.creator_tracker_row(LIST, a, n, "x", FIELDS, sent_date="2026-09-22")
            for a, n in (("dishsoap@evolved.gg", "Dishsoap"), ("zerbs@evolved.gg", "Zerbs"))]


def test_the_shared_agency_domain_maps_to_both_creators():
    assert rw.domain_brands(_creator_rows())["evolved.gg"] == ["Dishsoap", "Zerbs"]
    assert rw.exact_addresses(_creator_rows())["dishsoap@evolved.gg"] == "Dishsoap"


@pytest.fixture
def watch(tmp_path, monkeypatch):
    tracker = tmp_path / "prospect-tracker.csv"
    with open(tracker, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(_creator_rows())
    monkeypatch.setattr(rw, "TRACKER", str(tracker))
    monkeypatch.setattr(rw, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rw, "LOG", str(tmp_path / "watch.log"))
    nudges = []
    monkeypatch.setattr(rw, "nudge", lambda title, body, *a, **k: nudges.append((title, body)))
    monkeypatch.setattr(rw, "_beat", lambda note="": None)
    monkeypatch.setattr(rw, "arm_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(rw, "_refresh_funnel", lambda: None)   # would write the REAL vault report
    monkeypatch.setenv("COMPOSIO_API_KEY", "test")

    def run(sender):
        msgs = [{"messageId": "m1", "sender": sender, "subject": "re: clips",
                 "messageText": "Hey, sounds interesting. What would the first week look like?",
                 "payload": {"headers": [{"name": "From", "value": sender}]}}]
        tools = types.SimpleNamespace(execute=lambda *a, **k: {"data": {"messages": msgs}})
        mod = types.ModuleType("composio")
        mod.Composio = lambda api_key=None: types.SimpleNamespace(tools=tools)
        monkeypatch.setitem(sys.modules, "composio", mod)
        assert rw.main() == 0
        with open(tracker, newline="", encoding="utf-8") as f:
            return {r["brand"]: r["replied"] for r in csv.DictReader(f)}, nudges
    return run


def test_a_reply_from_dishsoaps_own_address_stamps_dishsoap_not_zerbs(watch):
    replied, nudges = watch("Dishsoap <dishsoap@evolved.gg>")
    assert replied["Dishsoap"] and not replied["Zerbs"]
    assert nudges and nudges[0][0] == "Dishsoap replied"


def test_a_reply_from_the_agency_itself_stamps_everyone_on_that_domain(watch):
    replied, nudges = watch("Manager <talent@evolved.gg>")
    assert replied["Dishsoap"] and replied["Zerbs"]
    assert "shared by Dishsoap / Zerbs" in nudges[0][1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
