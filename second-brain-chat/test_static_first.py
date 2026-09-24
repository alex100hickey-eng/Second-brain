"""Static-first: a NAMED first touch can carry the brand's static in the FIRST email.

Two locks, both required: STATIC_FIRST in offer_statics.py (default off) and Alex's `approve` in the
first-touch QA folder's INDEX.md. A first touch that hasn't been re-addressed to the founder the
variant was written for is left alone. No network: Composio and the queue are faked.
"""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("offer_statics_first", os.path.join(ROOT, "scripts", "offer_statics.py"))
ofs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ofs)

BODY = " ".join(["Brock, 282 five-star reviews on two of your ads, one from February and one from March."] * 4
                ) + " I made one to show what I mean. It's attached. Are the mixers or the sauces pulling more?"


@pytest.fixture
def qa(tmp_path):
    (tmp_path / "INDEX.md").write_text(
        "| brand | file | verdict (Alex) |\n|---|---|---|\n"
        "| High Mesa Chile Co. | `hm.png` | approve |\n| My Hair Dance | `mhd.png` |  |\n", encoding="utf-8")
    (tmp_path / "FIRST_TOUCH.md").write_text(
        f"## High Mesa Chile Co.\nto: brock@highmesachile.co\n```text\n{BODY}\n```\n\n"
        f"## My Hair Dance\nto: anna@myhairdance.com\n```text\n{BODY}\n```\n", encoding="utf-8")
    (tmp_path / "hm.png").write_bytes(b"png")
    (tmp_path / "mhd.png").write_bytes(b"png")
    return tmp_path


def test_it_is_off_by_default_and_touches_nothing_while_off(monkeypatch, capsys):
    assert ofs.STATIC_FIRST is False

    def boom():
        raise AssertionError("must not reach the network while STATIC_FIRST is off")
    monkeypatch.setattr(ofs, "_env", boom)
    assert ofs.main(["swap-first", "--apply"]) == 0
    assert "STATIC_FIRST is off" in capsys.readouterr().out


def test_only_approved_rendered_brands_qualify(qa):
    ok, skipped = ofs.approved_first_touch(str(qa))
    assert list(ok) == ["brock@highmesachile.co"]
    os.remove(qa / "hm.png")
    ok, skipped = ofs.approved_first_touch(str(qa))
    assert not ok and any("missing" in s for s in skipped), "not rendered means not sent"


def test_first_touches_get_a_longer_word_limit_than_follow_ups():
    long_body = " ".join(["word"] * 175)
    assert ofs.body_problems(long_body)                                  # too long for a follow-up
    assert not ofs.body_problems(long_body, ofs.FIRST_TOUCH_MAX_WORDS)   # fine for a first touch


def test_a_desk_addressed_draft_waits_for_the_readdress():
    queue = [{"brand": "High Mesa Chile Co.", "to": "info@highmesachile.co", "draft_id": "r1"}]
    entry, why = ofs.pending_first_touch(queue, "High Mesa Chile Co.", "brock@highmesachile.co")
    assert entry is None and "re-address" in why
    queue[0]["to"] = "brock@highmesachile.co"
    entry, why = ofs.pending_first_touch(queue, "High Mesa Chile Co.", "brock@highmesachile.co")
    assert entry is queue[0] and why == ""
    queue[0]["released"] = "2026-09-24T09:00"
    assert ofs.pending_first_touch(queue, "High Mesa Chile Co.", "brock@highmesachile.co")[0] is None


class _Composio:
    def __init__(self, to="brock@highmesachile.co", attached=True):
        self.to, self.attached, self.calls = to, attached, []
        self.tools, self.client = self, object()

    def execute(self, slug, user_id=None, dangerously_skip_version_check=None, arguments=None):
        self.calls.append((slug, arguments))
        if slug == "GMAIL_CREATE_EMAIL_DRAFT":
            return {"successful": True, "data": {"id": "r-new"}}
        return {"successful": True, "data": {"message": {
            "payload": {"headers": [{"name": "To", "value": self.to}]},
            "attachmentList": [{"filename": "hm.png"}] if self.attached else []}}}


def _up(path):
    return {"name": os.path.basename(path), "mimetype": "image/png", "s3key": "k"}


def test_the_new_first_touch_is_verified_before_use():
    comp = _Composio()
    assert ofs.create_first_touch_draft(comp, "studio", "brock@highmesachile.co", "s", BODY,
                                        "/qa/hm.png", upload=_up) == ("r-new", [])
    assert "thread_id" not in comp.calls[0][1], "a first touch is a new email, not a reply"
    _d, problems = ofs.create_first_touch_draft(_Composio(attached=False), "studio",
                                                "brock@highmesachile.co", "s", BODY, "/qa/hm.png", upload=_up)
    assert any("not attached" in p for p in problems)
    _d, problems = ofs.create_first_touch_draft(_Composio(to="info@highmesachile.co"), "studio",
                                                "brock@highmesachile.co", "s", BODY, "/qa/hm.png", upload=_up)
    assert any("founder" in p for p in problems)


def test_with_both_locks_open_the_queue_entry_is_repointed(qa, monkeypatch):
    monkeypatch.setattr(ofs, "STATIC_FIRST", True)
    comp = _Composio()
    monkeypatch.setattr(ofs, "_env", lambda: (None, None, comp, "studio"))
    monkeypatch.setattr(ofs, "create_first_touch_draft",
                        lambda c, e, to, s, b, png: ("r-new", []))
    queue = [{"brand": "High Mesa Chile Co.", "to": "brock@highmesachile.co", "draft_id": "r-old",
              "subject": "282 five-star reviews, twice", "body": "old"}]
    saved = []
    fake_sq = types.SimpleNamespace(load_queue=lambda: ({}, queue),
                                    save_queue=lambda q, qq: saved.append([dict(e) for e in qq]))

    monkeypatch.setattr(ofs, "_queue_module", lambda: fake_sq)
    assert ofs.main(["swap-first", "--apply", "--dir", str(qa)]) == 0
    e = saved[-1][0]
    assert (e["draft_id"], e["replaced_draft"], e["static_attached"]) == ("r-new", "r-old", "hm.png")
    assert e["body"].startswith("Brock,")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
