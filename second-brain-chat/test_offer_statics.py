"""The offer arm's promised statics reach a follow-up only when Alex approves, and only intact.

All 11 offer-arm first touches promised a free static "yours either way", and nothing built them.
Now they exist, and each brand has a follow-up written to carry one. These tests pin the
rules: no approval, no change. An attached draft must be a real reply (thread plus both reply
headers) with the PNG really on it, or it isn't used. And a static that's gone is never offered
again. No network: Composio and the model are faked.
"""
import ast
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(SCRIPTS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ofs = _load("offer_statics", "offer_statics_under_test")
sfd = _load("splitframe_daily", "splitframe_daily_statics")

INDEX = """# Offer-arm statics — QA

| brand | file | verdict (Alex) |
|---|---|---|
| Gracie's Doggie Delights | `gracie.png` | approve |
| Moon Juice | `moon.png` |  |
| Saltair | `saltair.png` | no |
| Geode Swimwear | `geode.png` | Approved, lovely |
"""

BODY = ("Made the one I said I would, it's attached. Built on your beef liver bag shot. It leads "
        "with the part only you have, one ingredient, and the line from your own about page under "
        "it. Yours to run or not. Do customers bring up the one-ingredient thing on their own?")

FOLLOWUPS = f"""# Follow-ups

## Gracie's Doggie Delights
to: Gracie@GraciesDoggieDelights.com
```text
{BODY}
```

## Moon Juice
to: press@moonjuice.com
```text
{BODY}
```

## Geode Swimwear
to: hello@geodeswimwear.com
```text
Short.
```
"""


@pytest.fixture
def qa(tmp_path):
    (tmp_path / "INDEX.md").write_text(INDEX, encoding="utf-8")
    (tmp_path / "FOLLOWUPS.md").write_text(FOLLOWUPS, encoding="utf-8")
    for f in ("gracie.png", "moon.png", "geode.png"):
        (tmp_path / f).write_bytes(b"\x89PNG fake")
    return tmp_path


# ---- Alex's verdicts ----

@pytest.mark.parametrize("v,ok", [("approve", True), ("Approved, lovely", True), ("yes", True),
                                  ("✅", True), ("", False), ("no", False), ("reject", False),
                                  ("maybe later", False)])
def test_only_an_explicit_yes_counts(v, ok):
    assert ofs.is_approved(v) is ok


def test_the_index_table_and_the_followups_parse(qa):
    idx = ofs.parse_index(INDEX)
    assert idx["gracie's doggie delights"]["file"] == "gracie.png"
    assert "brand" not in idx and len(idx) == 4
    fu = ofs.parse_followups(FOLLOWUPS)
    assert fu["gracie's doggie delights"]["to"] == "gracie@graciesdoggiedelights.com"
    assert fu["gracie's doggie delights"]["body"] == BODY


def test_nothing_changes_without_approval_and_a_broken_variant_is_refused(qa):
    ok, skipped = ofs.approved_statics(str(qa))
    assert list(ok) == ["gracie@graciesdoggiedelights.com"], "Moon Juice has no verdict, Saltair is a no"
    assert ok["gracie@graciesdoggiedelights.com"]["png"].endswith("gracie.png")
    assert any("Geode" in s and "words" in s for s in skipped), "approved, but its email is too short"


def test_an_approved_brand_with_no_png_is_skipped_and_says_why(qa):
    os.remove(qa / "gracie.png")
    ok, skipped = ofs.approved_statics(str(qa))
    assert not ok and any("missing" in s for s in skipped)


def test_no_qa_folder_means_no_statics_not_an_error(tmp_path):
    assert ofs.approved_statics(str(tmp_path / "gone")) == ({}, [f"cannot read the QA folder "
                                                            f"([Errno 2] No such file or directory: "
                                                            f"'{tmp_path / 'gone' / 'INDEX.md'}')"])
    assert ofs.latest_qa_dir(str(tmp_path / "gone")) == ""


@pytest.mark.parametrize("body,flag", [
    (BODY.replace("Made", "Our AI made"), "AI"),
    ("Too short to be a real follow-up.", "words"),
    (BODY + " {brand}", "unfinished"),
])
def test_the_variant_guard(body, flag):
    assert any(flag in p for p in ofs.body_problems(body))


def test_the_variant_guard_runs_the_fabrication_check():
    tell = sfd.FABRICATION_TELLS[0]
    assert any("claims work not done" in p for p in ofs.body_problems(BODY + " " + tell))


# ---- which follow-up changes ----

def test_plan_attach_once_then_never_offer_again():
    approved = {"a@x.com": {}}
    assert ofs.plan_for("A@x.com", {}, approved) == "attach"
    assert ofs.plan_for("a@x.com", {"a@x.com": {}}, approved) == "delivered"
    assert ofs.plan_for("b@x.com", {}, approved) == ""


def test_the_pending_followup_is_the_unsent_reply_not_the_first_touch():
    rows = [{"id": 1, "kind": "email_draft", "title": "Send the reply to a@x.com",
             "detail": "Subject: your ad account\n\nb"},
            {"id": 2, "kind": "email_draft", "title": "Send the reply to a@x.com",
             "detail": "Subject: Re: your ad account\n\nb", "sent_at": "2026-09-23T10:00"},
            {"id": 3, "kind": "email_draft", "title": "Send the reply to a@x.com",
             "detail": "Subject: Re: your ad account\n\nb", "static_attached": "a.png"},
            {"id": 4, "kind": "email_draft", "title": "Send the reply to a@x.com",
             "detail": "Subject: Re: your ad account\n\nb"}]
    assert ofs.pending_followup(rows, "a@x.com")["id"] == 4
    assert ofs.pending_followup(rows[:3], "a@x.com") is None


# ---- the draft: a real reply, really carrying the PNG ----

class _Composio:
    def __init__(self, headers=True, attached=True, thread="t1"):
        self.calls = []
        self.headers, self.attached, self.thread = headers, attached, thread
        self.tools = self
        self.client = object()

    def execute(self, slug, user_id=None, dangerously_skip_version_check=None, arguments=None):
        self.calls.append((slug, arguments))
        if slug == "GMAIL_CREATE_EMAIL_DRAFT":
            return {"successful": True, "data": {"id": "r-new"}}
        hdrs = [{"name": "To", "value": "a@x.com"}]
        if self.headers:
            hdrs += [{"name": "In-Reply-To", "value": "<m1>"}, {"name": "References", "value": "<m1>"}]
        return {"successful": True, "data": {"message": {
            "threadId": self.thread, "payload": {"headers": hdrs},
            "attachmentList": [{"filename": "gracie.png"}] if self.attached else []}}}


def _up(path):
    return {"name": os.path.basename(path), "mimetype": "image/png", "s3key": "k"}


def test_a_verified_reply_with_the_png_is_accepted():
    comp = _Composio()
    draft, problems = ofs.create_attach_draft(comp, "studio", "a@x.com", "Re: s", BODY, "t1",
                                              "/qa/gracie.png", upload=_up)
    assert (draft, problems) == ("r-new", [])
    args = comp.calls[0][1]
    assert args["thread_id"] == "t1" and args["attachment"]["name"] == "gracie.png"


@pytest.mark.parametrize("kw,why", [({"headers": False}, "reply headers"),
                                    ({"attached": False}, "not attached"),
                                    ({"thread": "other"}, "original thread")])
def test_a_draft_that_is_not_a_real_reply_or_lost_the_png_is_rejected(kw, why):
    _d, problems = ofs.create_attach_draft(_Composio(**kw), "studio", "a@x.com", "Re: s", BODY,
                                           "t1", "/qa/gracie.png", upload=_up)
    assert any(why in p for p in problems)


def test_no_thread_means_no_draft_at_all():
    comp = _Composio()
    _d, problems = ofs.create_attach_draft(comp, "studio", "a@x.com", "Re: s", BODY, "",
                                           "/qa/gracie.png", upload=_up)
    assert problems and comp.calls == []


# ---- the drafter ----

def test_the_next_touch_is_told_the_static_already_went():
    seen = {}

    class Msgs:
        def create(self, **kw):
            seen["ask"] = kw["messages"][0]["content"]
            return types.SimpleNamespace(content=[types.SimpleNamespace(
                type="text", text='{"body": "' + BODY + '"}')])
    client = types.SimpleNamespace(messages=Msgs())
    sfd.write_followup(client, "Gracie's", "", 3, "the first email", 7, extra=ofs.DELIVERED_NOTE)
    assert ofs.DELIVERED_NOTE in seen["ask"]
    sfd.write_followup(client, "Gracie's", "", 3, "the first email", 7)
    assert ofs.DELIVERED_NOTE not in seen["ask"]


def test_the_drafter_uses_the_statics_and_cannot_be_killed_by_them(monkeypatch):
    tree = ast.parse(open(os.path.join(SCRIPTS, "splitframe_daily.py"), encoding="utf-8").read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {getattr(c.func, "id", "") or getattr(c.func, "attr", "")
              for c in ast.walk(main) if isinstance(c, ast.Call)}
    assert {"load_offer_statics", "create_attach_draft", "plan_for"} <= called
    logged = []
    monkeypatch.setattr(sfd, "log", logged.append)

    def boom(*a, **k):
        raise RuntimeError("QA folder evicted by iCloud")
    monkeypatch.setattr(importlib.util, "spec_from_file_location", boom)
    assert sfd.load_offer_statics() == (None, {}, {})
    assert any("normal wording" in line for line in logged)


def test_arming_marks_the_row_so_a_swap_never_repeats_it():
    class Box:
        def __init__(self):
            self.rows = [{"id": 7, "title": "Send the reply to a@x.com"}]
            self.written, self.armed = {}, {}

        def open_items(self):
            return self.rows

        def _write(self, rid, changes):
            self.written[rid] = changes

        def arm_auto_send(self, rid, when):
            self.armed[rid] = when
    box = Box()
    assert sfd.arm_new_followup(box, "A@x.com", {"static_attached": "gracie.png"})
    assert box.written[7] == {"static_attached": "gracie.png"} and 7 in box.armed


# ---- the real QA folder, when this machine has it ----

def test_every_real_variant_parses_and_passes_the_guard():
    qa = ofs.latest_qa_dir()
    if not qa or not os.path.exists(os.path.join(qa, "FOLLOWUPS.md")):
        pytest.skip("no QA folder on this machine")
    idx = ofs.parse_index(open(os.path.join(qa, "INDEX.md"), encoding="utf-8").read())
    fu = ofs.parse_followups(open(os.path.join(qa, "FOLLOWUPS.md"), encoding="utf-8").read())
    assert set(idx) == set(fu), "every rendered static has exactly one follow-up"
    for key, v in fu.items():
        assert "@" in v["to"] and not ofs.body_problems(v["body"]), (key, ofs.body_problems(v["body"]))
        assert os.path.exists(os.path.join(qa, idx[key]["file"])), idx[key]["file"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
