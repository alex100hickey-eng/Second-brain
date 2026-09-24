"""An open outbox item must stay visible however many newer rows exist.

2026-09-24: Moon Juice's follow-up (static attached, auto-send 09:17) was the 35th newest row.
due_to_auto_send and awaiting_send read only the newest 30 rows of ANY status, so the sender never
saw it, and a tap on Send would have been read through the same window. Everything here runs on
an in-memory stand-in for the Supabase table.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("outbox_window", os.path.join(HERE, "outbox.py"))
outbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outbox)


class FakeSB:
    """select / eq / order / limit / insert / update over one in-memory table."""
    def __init__(self):
        self.rows, self._next, self.reads = [], 1, []

    def table(self, _name):
        return self

    def insert(self, row):
        row = dict(row, id=self._next)
        self._next += 1
        self.rows.append(row)
        self._result = [row]
        return self

    def select(self, *_a):
        self._result = list(self.rows)
        return self

    def eq(self, col, val):
        self._result = [r for r in self._result if r.get(col) == val]
        return self

    def order(self, col, desc=False):
        self._result = sorted(self._result, key=lambda r: r.get(col, 0), reverse=desc)
        return self

    def limit(self, n):
        self.reads.append(n)
        self._result = self._result[:n]
        return self

    def update(self, changes):
        for r in self._result:
            r.update(changes)
        return self

    def execute(self):
        return type("R", (), {"data": list(self._result)})()


def _file(sb, status="open", **extra):
    item = {"kind": "email_draft", "title": "Send the reply to x@y.com", "status": status,
            "created": datetime.now().isoformat(), "snooze_until": "", **extra}
    sb.insert({"agent_name": outbox.AGENT, "output_text": json.dumps(item)})
    return sb.rows[-1]["id"]


@pytest.fixture
def sb():
    s = FakeSB()
    outbox.init(s)
    yield s
    outbox.init(None)


def _due_old_then_newer(sb, newer=40, **old):
    past = (datetime.now() - timedelta(minutes=20)).isoformat()
    old_id = _file(sb, auto_send_at=past, **old)
    for _ in range(newer):
        _file(sb, status="done", sent_at=past)
    return old_id


def test_an_old_due_draft_is_still_sent_behind_forty_newer_rows(sb):
    old_id = _due_old_then_newer(sb)
    due = outbox.due_to_auto_send(datetime.now().isoformat())
    assert [it["id"] for it in due] == [old_id]


def test_a_tapped_send_is_still_seen_behind_forty_newer_rows(sb):
    old_id = _due_old_then_newer(sb, send_approved=True)
    assert [it["id"] for it in outbox.awaiting_send()] == [old_id]


def test_limit_counts_open_items_not_rows_read(sb):
    opens = [_file(sb) for _ in range(6)]
    for _ in range(70):
        _file(sb, status="done")
    got = outbox.open_items(limit=5)
    assert [it["id"] for it in got] == sorted(opens, reverse=True)[:5], "newest five OPEN items"


def test_a_small_table_is_read_once(sb):
    for _ in range(10):
        _file(sb)
    outbox.open_items()
    assert len(sb.reads) == 1, "no widening when every row was already read"


def test_the_ceiling_is_said_out_loud_not_silent(sb, monkeypatch, capsys):
    monkeypatch.setattr(outbox, "SCAN_CEILING", 60)
    old_id = _file(sb)
    for _ in range(80):
        _file(sb, status="done")
    assert outbox.open_items() == []
    assert "not visible" in capsys.readouterr().out
    assert old_id


def test_get_finds_an_item_older_than_any_window(sb):
    old_id = _file(sb, title="the old one")
    for _ in range(200):
        _file(sb, status="done")
    assert outbox.get(old_id)["title"] == "the old one"
    assert outbox.get(99999) is None


def test_no_store_still_fails_soft():
    outbox.init(None)
    assert outbox.open_items() == [] and outbox.get(1) is None
    assert outbox.due_to_auto_send(datetime.now().isoformat()) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
