"""reply_watch: a founder's reply gets the playbook answer drafted before Alex is told, once per
thread, and nothing is ever sent. No network: the drafter, the link and the phone are stand-ins."""
import importlib.util
import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout
spec = importlib.util.spec_from_file_location("reply_watch", os.path.join(ROOT, "scripts", "reply_watch.py"))
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)


class _Calls:
    def __init__(self, stdout="DRAFTED on the thread for Alex to send: Draft saved (draft id r1)"):
        self.stdout, self.runs, self.nudges = stdout, [], []

    def run(self, cmd, **kw):
        self.runs.append(cmd)
        return types.SimpleNamespace(stdout=self.stdout, stderr="")

    def notify(self, title, body):
        self.nudges.append((title, body))


def _handle(c, st, thread="t1"):
    return rw.handle_human_reply("Tree Juice", "jake@treejuice.com", thread, st, "Jake: how much?",
                                 run=c.run, link_for=lambda a: "https://x/do/abc", notify=c.notify)


def test_one_reply_one_draft_one_nudge(monkeypatch):
    monkeypatch.setattr(rw, "log", lambda *a: None)
    c, st = _Calls(), {}
    assert _handle(c, st) == "drafted"
    assert len(c.runs) == 1 and c.runs[0][-3:] == ["reply", "--thread", "t1"]
    assert "--dry-run" not in c.runs[0] and not any("send" in part for part in c.runs[0])
    assert c.nudges == [("Reply from Tree Juice: answer drafted, tap to send", "Jake: how much?\nhttps://x/do/abc")]
    assert st["drafted_threads"] == ["t1"]


def test_the_same_thread_is_never_drafted_twice(monkeypatch):
    monkeypatch.setattr(rw, "log", lambda *a: None)
    c, st = _Calls(), {}
    _handle(c, st)
    st.pop("_drafts_this_run", None)                    # a later run
    assert _handle(c, st) == "again"
    assert len(c.runs) == 1, "no second draft"
    assert c.nudges[-1][0] == "Tree Juice replied again", "but a new reply is never silent"


def test_a_refused_draft_still_tells_alex_and_can_be_retried(monkeypatch):
    monkeypatch.setattr(rw, "log", lambda *a: None)
    c, st = _Calls(stdout="NOT drafted:\n  - a price outside the price story"), {}
    assert _handle(c, st) == "failed"
    assert "reply --thread t1" in c.nudges[0][1]
    assert "t1" not in st.get("drafted_threads", [])


def test_at_most_two_drafts_per_run_so_the_watchdog_never_fires(monkeypatch):
    monkeypatch.setattr(rw, "log", lambda *a: None)
    c, st = _Calls(), {}
    assert [_handle(c, st, t) for t in ("a", "b", "c")] == ["drafted", "drafted", "failed"]
    assert len(c.runs) == 2 and "reply --thread c" in c.nudges[-1][1]
