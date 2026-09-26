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
    # The flag is a bool Alex flips (on since 2026-09-25, his approval of every static);
    # the contract under test is that OFF touches nothing, so pin it off for the test.
    assert isinstance(ofs.STATIC_FIRST, bool)
    monkeypatch.setattr(ofs, "STATIC_FIRST", False)

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


class _State:
    """intake's _load_state/_save_state over one dict."""
    def __init__(self, data=None):
        self.data = data or {}

    def _load_state(self, key):
        return dict(self.data.get(key) or {})

    def _save_state(self, state):
        self.data[state["key"]] = dict(state)


def test_with_both_locks_open_the_queue_entry_is_repointed(qa, monkeypatch):
    monkeypatch.setattr(ofs, "STATIC_FIRST", True)
    comp = _Composio()
    store = _State()
    monkeypatch.setattr(ofs, "_env", lambda: (store, None, comp, "studio"))
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
    # the follow-up drafter must see it as delivered, or FU1 attaches the same static again
    d = store.data[ofs.STATE_KEY]["delivered"]["brock@highmesachile.co"]
    assert d["via"] == "first-touch" and d["file"] == "hm.png"
    assert ofs.plan_for("brock@highmesachile.co", store.data[ofs.STATE_KEY]["delivered"],
                        {"brock@highmesachile.co": {}}) == "delivered"


def test_an_already_delivered_static_is_never_swapped_in_twice(qa, monkeypatch, capsys):
    monkeypatch.setattr(ofs, "STATIC_FIRST", True)
    store = _State({ofs.STATE_KEY: {"delivered": {"brock@highmesachile.co": {
        "via": "swap", "at": "2026-09-24T09:10"}}}})
    monkeypatch.setattr(ofs, "_env", lambda: (store, None, _Composio(), "studio"))
    created = []
    monkeypatch.setattr(ofs, "create_first_touch_draft", lambda *a: created.append(a) or ("r-x", []))
    queue = [{"brand": "High Mesa Chile Co.", "to": "brock@highmesachile.co", "draft_id": "r-old",
              "subject": "s", "body": "old"}]
    monkeypatch.setattr(ofs, "_queue_module", lambda: types.SimpleNamespace(
        load_queue=lambda: ({}, queue), save_queue=lambda q, qq: None))
    assert ofs.main(["swap-first", "--apply", "--dir", str(qa)]) == 0
    assert created == [] and "already delivered" in capsys.readouterr().out

def test_the_0730_backstop_swaps_from_the_git_mirror_before_the_release():
    """The server releases first touches at 07:50. The Mac backstop must run before that, read the
    QA folder from the vault's git mirror (iCloud evicts it), and only ever call swap-first,
    which is a no-op until STATIC_FIRST is on and Alex has marked a brand approve."""
    import plistlib
    script = open(os.path.join(ROOT, "scripts", "static_first_backstop.sh"), encoding="utf-8").read()
    assert ".second-brain-vault.git" in script and "first-touch-qa-" in script
    assert "offer_statics.py swap-first --apply --dir" in script
    with open(os.path.join(ROOT, "scripts", "com.secondbrain.staticfirst.plist"), "rb") as f:
        plist = plistlib.load(f)
    when = plist["StartCalendarInterval"]
    assert (when["Hour"], when["Minute"]) < (7, 50)
    assert plist["ProgramArguments"][-1].endswith("scripts/static_first_backstop.sh")



# ---------------------------------------------------------------------------
# Which folder the 07:30 swap reads (2026-09-26).
#
# Folders are named for the day their first touches go out and are built days ahead. "Newest"
# on Monday 09-28 was already Wednesday's first-touch-qa-2026-09-30, which has no Monday brand in
# it: Monday's five arm-A statics would never have attached, and arm A would have gone out as
# arm B's plain permission email with nothing in any log saying the A/B had collapsed.
# ---------------------------------------------------------------------------

FOLDERS = ("first-touch-qa-2026-09-24", "first-touch-qa-2026-09-28", "first-touch-qa-2026-09-29",
           "first-touch-qa-2026-09-30", "qa-2026-09-23")


@pytest.mark.parametrize("today, want", [
    ("2026-09-28", "first-touch-qa-2026-09-28"),
    ("2026-09-29", "first-touch-qa-2026-09-29"),
    ("2026-10-01", "first-touch-qa-2026-09-30"),     # Thursday's statics ride in Wednesday's folder
    ("2026-09-27", "first-touch-qa-2026-09-24"),
    ("2026-09-20", ""),
])
def test_the_swap_reads_the_newest_folder_dated_today_or_earlier(tmp_path, today, want):
    for d in FOLDERS:
        (tmp_path / d).mkdir()
    got = ofs.first_touch_dir(str(tmp_path), today)
    assert os.path.basename(got) == want
    assert os.path.basename(ofs.latest_qa_dir(str(tmp_path))) == "qa-2026-09-23", "the follow-up swap is unchanged"


PY = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"


@pytest.mark.skipif(not os.path.exists(PY), reason="the backstop's own python is not on this machine")
def test_the_0730_backstop_picks_the_same_folder(tmp_path):
    """The real script, against a throwaway vault mirror and a stub offer_statics.py."""
    import subprocess
    home = tmp_path / "home"
    src = tmp_path / "src"
    for d, marker in (("first-touch-qa-2026-09-28", "MONDAY"), ("first-touch-qa-2026-09-30", "WEDNESDAY")):
        folder = src / "Money" / "Clients" / "spec-ads" / d
        folder.mkdir(parents=True)
        (folder / "INDEX.md").write_text(marker, encoding="utf-8")
    git = ["git", "--git-dir", str(home / ".second-brain-vault.git"), "--work-tree", str(src)]
    subprocess.run(["git", "init", "-q", "--bare", str(home / ".second-brain-vault.git")], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v"], check=True)
    stub = home / "second-brain" / "scripts"
    stub.mkdir(parents=True)
    (stub / "offer_statics.py").write_text(
        "import os, sys\nd = sys.argv[sys.argv.index('--dir') + 1]\n"
        "print('ARGS', ' '.join(sys.argv[1:3]), open(os.path.join(d, 'INDEX.md')).read())\n")
    script = os.path.join(ROOT, "scripts", "static_first_backstop.sh")

    def run(today):
        env = dict(os.environ, HOME=str(home), STATIC_FIRST_TODAY=today)
        return subprocess.run(["/bin/zsh", script], env=env, capture_output=True, text=True, timeout=60).stdout
    out = run("2026-09-28")
    assert "using Money/Clients/spec-ads/first-touch-qa-2026-09-28" in out and "ARGS swap-first --apply MONDAY" in out
    assert "WEDNESDAY" in run("2026-10-01")
    out = run("2026-09-27")
    assert "no first-touch-qa-* folder dated 2026-09-27 or earlier" in out and "ARGS" not in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
