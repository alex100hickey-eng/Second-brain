"""The 2026-09-28 A/B: short first touches for named founders (--short), the arm recorded where
the funnel can attribute a reply (--arm), and a queued draft kept for a planned day
(--hold-until) even though the release is FIFO. No network: tracker, queue and Gmail stubbed."""
import copy
import importlib.util
import os
import types
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sq = _load("splitframe_queue")
sfd = sq._sfd

FORTY = ("Jake,\n\n\"Your pancakes will never be the same\" opens all five of your live ads. It's a good "
         "line. I make ad creative for DTC brands and built a static around your Banana Maple. "
         "Want me to send it over?\n\nAlex Hickey, Splitframe Studio")


def _row(**kw):
    r = {"brand": "Tree Juice", "domain": "treejuice.com", "email": "jake@treejuice.com",
         "email_generic": "hello@treejuice.com", "contact_name": "Jake Smith", "status": "qualified",
         "sent_date": "", "replied": "", "outcome": "", "close_variant": ""}
    r.update(kw)
    return r


def test_the_floor_is_80_unless_short():
    assert 35 <= len(FORTY.split()) < 80
    assert any("too short" in p for p in sq.guard_body(FORTY))
    assert not any("too short" in p for p in sq.guard_body(FORTY, sq.SHORT_MIN_WORDS))
    assert any("too short" in p for p in sq.guard_body("Jake, want the ad?", sq.SHORT_MIN_WORDS))


def test_short_passes_for_a_named_founder(monkeypatch):
    monkeypatch.setattr(sq, "subject_problem", lambda s: "")
    _row_, problems = sq.plan_add([_row()], [], "jake@treejuice.com", "Banana Maple", FORTY, 12,
                                  short=True)
    assert problems == []
    _row_, problems = sq.plan_add([_row()], [], "jake@treejuice.com", "Banana Maple", FORTY, 12)
    assert any("too short" in p for p in problems)


def test_short_is_refused_for_a_front_desk(monkeypatch):
    monkeypatch.setattr(sq, "subject_problem", lambda s: "")
    monkeypatch.setattr(sfd, "NAMED_ONLY", False)            # so the refusal is --short's own
    _row_, problems = sq.plan_add([_row()], [], "hello@treejuice.com", "Banana Maple", FORTY, 12,
                                  short=True)
    assert any("--short is only for a named founder" in p for p in problems)


def test_hold_until_must_be_a_date():
    assert sq.hold_until_problem("") == ""
    assert sq.hold_until_problem("2026-09-28") == ""
    assert "not a YYYY-MM-DD date" in sq.hold_until_problem("Monday")


def test_an_arm_drops_the_offer_photo_requirement():
    entry = {"to": "jake@treejuice.com", "draft_id": "r1", "close_variant": "offer", "offer_image": ""}
    _e, problems = sq.plan_revise([entry], "jake@treejuice.com", None, None, None, "treejuice.com")
    assert problems, "an offer with no photo is still refused without an arm"
    _e, problems = sq.plan_revise([entry], "jake@treejuice.com", None, None, None, "treejuice.com",
                                  arm="B")
    assert problems == []


class _State:
    def __init__(self, queue):
        self.rows = {sfd.QUEUE_KEY: {"key": sfd.QUEUE_KEY, "queue": copy.deepcopy(queue)}}

    def _load_state(self, key):
        return copy.deepcopy(self.rows.get(key))

    def _save_state(self, st):
        self.rows[st["key"]] = copy.deepcopy(st)


class _Outbox:
    n = 0

    def open_items(self, limit=60):
        return []

    def add(self, kind, title, **kw):
        _Outbox.n += 1
        return _Outbox.n

    def arm_auto_send(self, item_id, when_iso):
        return None


def test_the_release_skips_a_held_entry_until_its_day(monkeypatch):
    today = datetime.now(sfd.LOCAL_TZ).date()
    queue = [{"brand": "Held", "to": "a@x.com", "draft_id": "r1",
              "hold_until": (today + timedelta(days=1)).isoformat()},
             {"brand": "Free", "to": "b@x.com", "draft_id": "r2"},
             {"brand": "Due", "to": "c@x.com", "draft_id": "r3", "hold_until": today.isoformat()}]
    state = _State(queue)
    lines = []
    monkeypatch.setattr(sfd, "_shared", state)
    monkeypatch.setattr(sfd, "log", lines.append)
    monkeypatch.setattr(sfd, "tracker_rows", lambda: [])
    monkeypatch.setattr(sfd, "front_desk_hold", lambda entry, by: "")
    monkeypatch.setattr(sfd, "_queued_age_days", lambda entry: 0.0)
    out = sfd.release_first_touches(_Outbox(), "https://mail", limit=10)
    assert out == ["Free", "Due"]
    assert any("kept for a later day on purpose" in l and "Held" in l for l in lines)
    assert not state.rows[sfd.QUEUE_KEY]["queue"][0].get("released")


def test_revise_records_the_arm_and_the_hold(monkeypatch, tmp_path):
    entry = {"brand": "Tree Juice", "to": "jake@treejuice.com", "draft_id": "r1", "subject": "s",
             "body": "b", "close_variant": "offer", "offer_image": ""}
    state = _State([entry])
    rows = [_row()]
    written = {}
    monkeypatch.setattr(sq, "_intake", lambda: state)
    monkeypatch.setattr(sq, "tracker_rows", lambda: (rows, list(rows[0])))
    monkeypatch.setattr(sq, "write_tracker", lambda r, f, tag: written.setdefault(tag, copy.deepcopy(r)))
    monkeypatch.setattr(sq, "update_studio_draft", lambda *a: (True, "draft updated in place"))
    monkeypatch.setattr(sq, "subject_problem", lambda s: "")
    body = tmp_path / "b.txt"
    body.write_text(FORTY)
    args = types.SimpleNamespace(to="jake@treejuice.com", subject=None, body_file=str(body),
                                 offer_image=None, why="A/B arm B", new_to=None, dry_run=False,
                                 short=True, arm="b", hold_until="2026-09-28")
    assert sq.cmd_revise(args) == 0
    e = state.rows[sfd.QUEUE_KEY]["queue"][0]
    assert (e["arm"], e["close_variant"], e["hold_until"]) == ("B", "arm-B", "2026-09-28")
    assert written["arm"][0]["close_variant"] == "arm-B"
    args.hold_until, args.body_file, args.short, args.arm = "", None, False, None
    assert sq.cmd_revise(args) == 0
    assert "hold_until" not in state.rows[sfd.QUEUE_KEY]["queue"][0]


CREATOR_LIST = """# Creator lane — prospect list

### Guzu
- **Platform:** Twitch `twitch.tv/guzu`
- **Email:** `guzubusiness@hotmail.com` — read directly off his own Twitch About panel.
"""
SAMPLE_FIRST = ("Guzu,\n\nCut your Tuesday clutch into a 40 second vertical with captions. It's attached. "
                "Two fans already clipped the same ten seconds, so it travels. I edit clips for "
                "streamers. Want the next five from this week?\n\nAlex Hickey, Splitframe Studio")


def test_short_works_on_the_creator_command_too(monkeypatch):
    """Lane B's sample-first template is about 40 words: the same 35-word floor, same guards."""
    monkeypatch.setattr(sq, "subject_problem", lambda s: "")
    ev = "watched the 09-22 stream, the clutch at 1:14:05"
    assert 35 <= len(SAMPLE_FIRST.split()) < 80
    _e, problems = sq.plan_creator(CREATOR_LIST, [], "guzubusiness@hotmail.com", "your clutch",
                                   SAMPLE_FIRST, ev, True)
    assert any("too short" in p for p in problems)
    _e, problems = sq.plan_creator(CREATOR_LIST, [], "guzubusiness@hotmail.com", "your clutch",
                                   SAMPLE_FIRST, ev, True, short=True)
    assert problems == []
    _e, problems = sq.plan_creator(CREATOR_LIST, [], "guzubusiness@hotmail.com", "your clutch",
                                   "Guzu, want a clip?", ev, True, short=True)
    assert any("too short" in p for p in problems)
