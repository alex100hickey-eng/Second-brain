"""scripts/hunter_starter_pass.py: after buying Hunter Starter, one command verifies every waiting
founder address, stalest ads first, and never the reserve. No network."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout
spec = importlib.util.spec_from_file_location("hunter_starter_pass", os.path.join(ROOT, "scripts", "hunter_starter_pass.py"))
hsp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsp)


def _row(brand, ev, status="candidate", email="x@y.com"):
    return {"brand": brand, "named_email": email, "email_status": status, "email_evidence": ev}


def test_stalest_first_and_only_the_ones_still_waiting():
    rows = [_row("Fresh", "stale 2/24 oldest 490d"),
            _row("AllOld", "stale 6/6 oldest 760d"),
            _row("NoData", "first-name@ pattern"),
            _row("Mostly", "stale 26/27 oldest 444d"),
            _row("Risky", "stale 9/9 oldest 900d | Hunter 2026-09-25: risky (score 71)"),
            _row("Done", "stale 9/9", status="verified"),
            _row("NoAddr", "stale 9/9", email="")]
    assert [r["brand"] for r in hsp.waiting(rows)] == ["AllOld", "Mostly", "Fresh", "NoData"]


def test_it_never_runs_on_the_reserve():
    assert "never spends the reserve" in hsp.guard(500, {"HUNTER_USE_RESERVE": "1"}, 100)
    assert "can't be read" in hsp.guard(None, {}, 100)
    assert "Starter isn't active" in hsp.guard(15, {}, 100)
    assert hsp.guard(1000, {}, 100) == ""


def test_a_dry_run_spends_nothing(monkeypatch, capsys):
    import sys, types
    calls = []
    fake_cf = types.SimpleNamespace(HUNTER_RESERVE=15, verifications_left=lambda: 400,
                                    verify=lambda e: calls.append(e) or ("deliverable", 99),
                                    NOT_CHECKED="not-checked", SENDABLE="deliverable", UNDELIVERABLE="undeliverable")
    monkeypatch.setitem(sys.modules, "contact_finder", fake_cf)
    monkeypatch.setattr(hsp.sq, "read_named", lambda: [_row("A", "stale 1/1"), _row("B", "stale 1/2")])
    monkeypatch.setattr(hsp.sq, "write_named", lambda rows: calls.append("WRITE"))
    assert hsp.main(["--dry-run"]) == 0
    assert calls == [] and "may spend 385" in capsys.readouterr().out


def test_a_real_run_spends_at_most_the_budget(monkeypatch):
    import sys, types
    calls = []
    fake_cf = types.SimpleNamespace(HUNTER_RESERVE=15, verifications_left=lambda: 117,
                                    verify=lambda e: calls.append(e) or ("deliverable", 99),
                                    NOT_CHECKED="not-checked", SENDABLE="deliverable", UNDELIVERABLE="undeliverable")
    monkeypatch.setitem(sys.modules, "contact_finder", fake_cf)
    monkeypatch.delenv("HUNTER_USE_RESERVE", raising=False)
    rows = [_row(f"B{i}", f"stale {i}/10", email=f"a{i}@b{i}.com") for i in range(10)]
    monkeypatch.setattr(hsp.sq, "read_named", lambda: rows)
    monkeypatch.setattr(hsp.sq, "write_named", lambda r: None)
    monkeypatch.setattr(hsp.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="tracker written", stderr=""))
    assert hsp.main(["--limit", "3"]) == 0
    assert calls == ["a9@b9.com", "a8@b8.com", "a7@b7.com"]
    assert sum(r["email_status"] == "verified" for r in rows) == 3
