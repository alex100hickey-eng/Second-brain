"""The last 15 Hunter verifications of a cycle are the reserve (for re-addressing a founder who
replied). Read from the account's live balance before every spend. No network: _fetch is faked."""
import importlib.util
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("contact_finder_reserve", os.path.join(HERE, "contact_finder.py"))
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)


def _fake(remaining, verdict="deliverable"):
    calls = []

    def fetch(url):
        path = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "account":
            if remaining is None:
                raise RuntimeError("HTTP Error 500")
            return {"data": {"requests": {"verifications": {"used": 100 - remaining, "available": 100,
                                                            "remaining": remaining}}}}
        if path == "email-verifier":
            return {"data": {"result": verdict, "score": 95}}
        if path == "domain-search":
            return {"data": {"emails": []}}
        raise AssertionError(path)
    fetch.calls = calls
    return fetch


def test_above_the_reserve_it_verifies(monkeypatch):
    monkeypatch.delenv("HUNTER_USE_RESERVE", raising=False)
    f = _fake(16)
    monkeypatch.setattr(cf, "_fetch", f)
    assert cf.verify("jo@brand.com") == (cf.SENDABLE, 95)
    assert f.calls == ["account", "email-verifier"]


def test_at_the_reserve_it_spends_nothing(monkeypatch):
    monkeypatch.delenv("HUNTER_USE_RESERVE", raising=False)
    f = _fake(15)
    monkeypatch.setattr(cf, "_fetch", f)
    assert cf.verify("jo@brand.com") == (cf.NOT_CHECKED, 0)
    assert cf.find_for_domain("brand.com")["status"] == cf.NOT_CHECKED
    assert "email-verifier" not in f.calls and "domain-search" not in f.calls


def test_an_unreadable_balance_protects_the_reserve(monkeypatch):
    monkeypatch.delenv("HUNTER_USE_RESERVE", raising=False)
    f = _fake(None)
    monkeypatch.setattr(cf, "_fetch", f)
    assert cf.verify("jo@brand.com") == (cf.NOT_CHECKED, 0)
    assert "email-verifier" not in f.calls


def test_the_reserve_opens_only_on_purpose(monkeypatch):
    monkeypatch.setenv("HUNTER_USE_RESERVE", "1")
    f = _fake(3)
    monkeypatch.setattr(cf, "_fetch", f)
    assert cf.verify("jo@brand.com") == (cf.SENDABLE, 95)
