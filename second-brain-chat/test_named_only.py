"""Named person or no send (NAMED_ONLY, splitframe_daily.py), adopted 2026-09-23.

Two of the front-desk first touches, Calypsa and Geode, landed in Gorgias support queues, where
the reader is paid to close tickets rather than buy ad creative. So for the ad-creative lane a
front desk now waits for a founder's own address: held at release, refused at `add`, and left
off the draft list. The creator lane is exempt, because a streamer's business inbox is the
person. Every outbound path is faked here, and no test touches the real tracker or queue.
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sfd = _load("splitframe_daily", "splitframe_daily_named_only")
sq = _load("splitframe_queue", "splitframe_queue_named_only")


def _row(**kw):
    r = {"brand": "Loudcup", "domain": "theloudcup.com", "category": "snacks", "status": "qualified",
         "email": "", "email_generic": "hello@theloudcup.com", "contact_name": "",
         "sent_date": "", "replied": "", "outcome": "", "adlib_url": "", "notes": ""}
    r.update(kw)
    return r


def _entry(to, brand="Loudcup", **kw):
    e = {"brand": brand, "to": to, "draft_id": f"r-{brand}", "subject": "s", "body": "b",
         "queued_at": (datetime.now(sfd.LOCAL_TZ) - timedelta(hours=6)).isoformat()}
    e.update(kw)
    return e


# ---- who counts as a person ----

@pytest.mark.parametrize("row,addr,named", [
    (_row(contact_name="Mark Reyes"), "mark@acheerfulgiver.com", True),   # the row's own contact
    (_row(), "jennifer@tasteswoon.com", True),                            # a known first name
    (_row(), "maxx.appelman@trulybeauty.com", True),                      # first.last
    (_row(contact_name="Josh Lee"), "adventure@montanadogfoodco.com", False),  # named front desk
    (_row(), "hello@theloudcup.com", False),
    (_row(), "support@x.com", False),
    (None, "", False),
])
def test_what_counts_as_a_named_address(row, addr, named):
    assert sfd.is_named_address(row, addr) is named


# ---- the hold at release ----

def test_a_front_desk_is_held_a_person_and_a_creator_are_not():
    rows = sfd.rows_by_address([_row(), _row(brand="Cheerful", email="mark@acheerfulgiver.com",
                                             email_generic="", contact_name="Mark")])
    assert "Loudcup" in sfd.front_desk_hold(_entry("hello@theloudcup.com"), rows)
    assert sfd.front_desk_hold(_entry("mark@acheerfulgiver.com", "Cheerful"), rows) == ""
    assert sfd.front_desk_hold(_entry("contact@misterarther.com", "MISTERARTHER", lane="creator"),
                               rows) == ""


def test_the_hold_says_when_a_named_address_has_since_been_found():
    rows = sfd.rows_by_address([_row(email="anna@theloudcup.com", contact_name="Anna Park")])
    why = sfd.front_desk_hold(_entry("hello@theloudcup.com"), rows)
    assert "anna@theloudcup.com" in why and "re-draft" in why


def test_switching_it_off_restores_front_desk_sends(monkeypatch):
    monkeypatch.setattr(sfd, "NAMED_ONLY", False)
    assert sfd.front_desk_hold(_entry("hello@theloudcup.com"), {}) == ""


class _Shared:
    def __init__(self, queue):
        self.state = {"queue": [dict(e) for e in queue]}

    def _load_state(self, key):
        return self.state

    def _save_state(self, st):
        self.state = st


class _Outbox:
    def __init__(self):
        self.added, self._id = [], 100

    def open_items(self, limit=60):
        return []

    def add(self, kind, title, **kw):
        self._id += 1
        self.added.append(title)
        return self._id

    def arm_auto_send(self, item_id, when):
        return {"id": item_id}


def test_release_holds_front_desks_and_lets_people_and_creators_go(monkeypatch, tmp_path):
    queue = [_entry("hello@theloudcup.com"),
             _entry("mark@acheerfulgiver.com", "Cheerful"),
             _entry("contact@misterarther.com", "MISTERARTHER", lane="creator")]
    shared = _Shared(queue)
    logged = []
    monkeypatch.setattr(sfd, "_shared", shared)
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 0)
    monkeypatch.setattr(sfd, "log", logged.append)
    monkeypatch.setattr(sfd, "tracker_rows", lambda: [
        _row(), _row(brand="Cheerful", email="mark@acheerfulgiver.com", email_generic="",
                     contact_name="Mark")])
    box = _Outbox()
    released = sfd.release_first_touches(box, "https://mail")
    assert released == ["Cheerful", "MISTERARTHER"]
    assert any("named person or no send" in line and "Loudcup" in line for line in logged)
    held = next(e for e in shared.state["queue"] if e["brand"] == "Loudcup")
    assert not held.get("released"), "held, not retired: it goes once a named address exists"


def test_an_unreadable_tracker_still_holds_an_obvious_front_desk(monkeypatch):
    def boom():
        raise OSError("iCloud evicted it")
    shared = _Shared([_entry("hello@theloudcup.com"), _entry("jennifer@tasteswoon.com", "Swoon")])
    monkeypatch.setattr(sfd, "_shared", shared)
    monkeypatch.setattr(sfd, "current_cap", lambda: (10, "pinned"))
    monkeypatch.setattr(sfd, "followups_due_today", lambda *a, **k: 0)
    monkeypatch.setattr(sfd, "log", lambda m: None)
    monkeypatch.setattr(sfd, "tracker_rows", boom)
    assert sfd.release_first_touches(_Outbox(), "https://mail") == ["Swoon"]


# ---- the draft list and the add guard ----

def test_the_draft_list_offers_only_named_addresses():
    rows = [_row(), _row(brand="Cheerful", domain="acheerfulgiver.com", email="mark@acheerfulgiver.com",
                         email_generic="", contact_name="Mark")]
    assert [t["brand"] for t in sq.next_targets(rows, [])] == ["Cheerful"]


def test_add_refuses_a_front_desk_with_the_reason():
    _row_, problems = sq.plan_add([_row()], [], "hello@theloudcup.com", "s", "b", 12)
    assert any("named person or no send" in p for p in problems)
    _row_, problems = sq.plan_add([_row(email="anna@theloudcup.com", contact_name="Anna Park")], [],
                                  "anna@theloudcup.com", "s", "b", 12)
    assert not any("named person" in p for p in problems)


# ---- the side file: names and addresses researched for front desks ----

def _p(**kw):
    p = {"brand": "High Mesa Chile Co.", "domain": "highmesachile.co", "contact_name": "Brock Giles",
         "contact_title": "Founder", "name_source": "https://example.com/story",
         "named_email": "brock@highmesachile.co", "email_status": "published",
         "email_evidence": "Scovie directory", "researched": "2026-09-23"}
    p.update(kw)
    return p


def _hm(**kw):
    return _row(brand="High Mesa Chile Co.", domain="highmesachile.co",
                email_generic="info@highmesachile.co", **kw)


def test_a_published_founder_address_becomes_the_send_address_and_the_desk_is_kept():
    ((row, changes, notes),) = sq.plan_named([_hm()], [_p()])
    assert changes["email"] == "brock@highmesachile.co" and changes["contact_name"] == "Brock Giles"
    row.update(changes)
    assert sfd.target_address(row) == ("brock@highmesachile.co", "person")
    assert row["email_generic"] == "info@highmesachile.co", "the front desk is kept, not lost"


def test_a_guessed_address_is_never_applied_only_the_name_is():
    ((row, changes, notes),) = sq.plan_named([_hm()], [_p(email_status="candidate")])
    assert "email" not in changes and changes["contact_name"] == "Brock Giles"
    assert any("unverified guess" in n for n in notes)


def test_a_name_never_replaces_a_different_person_but_extends_a_first_name():
    ((_r, changes, notes),) = sq.plan_named([_hm(contact_name="Dana")], [_p()])
    assert "contact_name" not in changes and any("conflict" in n for n in notes)
    ((_r, changes, _n),) = sq.plan_named([_hm(contact_name="Brock")], [_p()])
    assert changes["contact_name"] == "Brock Giles"


def test_an_address_off_the_brands_own_domain_is_refused():
    ((_r, changes, notes),) = sq.plan_named([_hm()], [_p(named_email="brock@gmail.com")])
    assert "email" not in changes and any("own mail domain" in n for n in notes)


def test_verify_promotes_only_a_positive_result():
    props = [_p(named_email="a@x.co", email_status="candidate"),
             _p(named_email="b@x.co", email_status="candidate"),
             _p(named_email="c@x.co", email_status="candidate"),
             _p(named_email="d@x.co", email_status="published")]
    results = {"a@x.co": ("deliverable", 96), "b@x.co": ("undeliverable", 0),
               "c@x.co": ("risky", 50)}
    calls = []

    def fake(email):
        calls.append(email)
        return results[email]
    changed = sq.verify_candidates(props, fake)
    assert changed == [("a@x.co", "verified"), ("b@x.co", "rejected")]
    assert [p["email_status"] for p in props] == ["verified", "rejected", "candidate", "published"]
    assert "risky" in props[2]["email_evidence"], "accept-all is recorded, not trusted"
    assert calls == ["a@x.co", "b@x.co", "c@x.co"], "a published address costs no verification"


def test_readdressing_stays_on_the_same_brand_and_needs_a_named_address():
    rows = [_hm(email="brock@highmesachile.co", contact_name="Brock Giles"),
            _row(brand="Other", email_generic="hi@other.com", domain="other.com")]
    queue = [_entry("info@highmesachile.co", "High Mesa Chile Co.")]
    assert sq.plan_readdress(rows, queue, "info@highmesachile.co", "brock@highmesachile.co") == []
    assert sq.plan_readdress(rows, queue, "info@highmesachile.co", "hi@other.com")
    assert any("not on" in p for p in
               sq.plan_readdress(rows, queue, "info@highmesachile.co", "ceo@highmesachile.co"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
