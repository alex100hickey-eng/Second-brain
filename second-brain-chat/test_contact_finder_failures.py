"""Hunter not answering is not an answer.

2026-09-24: Hunter's free quota hadn't rolled on its reset date, every verify call failed, and each
failure read as RISKY: 53 candidates were stamped with a result nobody had got. The same failure in a
domain search read as NONE_FOUND, and a row stamped none-found with a checked date is never searched
again. Everything here runs through contact_finder's one network seam (_fetch); nothing touches
Hunter or the real tracker.
"""
import csv
import importlib.util
import os
import sys
import urllib.parse

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("contact_finder_failures", os.path.join(HERE, "contact_finder.py"))
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)


@pytest.fixture(autouse=True)
def _reserve_off(monkeypatch):
    """These tests are about what a failed or answered call MEANS; the reserve (a separate account
    call, tested in test_hunter_reserve.py) would add a request to every fake."""
    monkeypatch.setattr(cf, "reserve_blocks", lambda: "")


def _fake(answers):
    """answers: {(path, key): response dict | Exception}; key is the domain or email asked about."""
    calls = []

    def fetch(url):
        parts = urllib.parse.urlparse(url)
        path = parts.path.rsplit("/", 1)[-1]
        q = urllib.parse.parse_qs(parts.query)
        key = (q.get("domain") or q.get("email") or [""])[0]
        calls.append((path, key))
        ans = answers.get((path, key), RuntimeError("HTTP Error 429: Too Many Requests"))
        if isinstance(ans, Exception):
            raise ans
        return ans
    fetch.calls = calls
    return fetch


def _found(*emails):
    return {"data": {"emails": [{"value": e, "first_name": "Jo", "last_name": "Lee", "position": "Founder",
                                 "confidence": 90, "type": "personal"} for e in emails]}}


def _verdict(result, score=90):
    return {"data": {"result": result, "score": score}}


def test_a_failed_verify_is_not_checked_never_risky(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({}))
    assert cf.verify("jo@brand.com") == (cf.NOT_CHECKED, 0)


def test_real_verdicts_are_unchanged(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({
        ("email-verifier", "a@x.com"): _verdict("deliverable", 97),
        ("email-verifier", "b@x.com"): _verdict("undeliverable", 0),
        ("email-verifier", "c@x.com"): _verdict("risky", 40)}))
    assert cf.verify("a@x.com") == (cf.SENDABLE, 97)
    assert cf.verify("b@x.com") == (cf.UNDELIVERABLE, 0)
    assert cf.verify("c@x.com") == (cf.RISKY, 40), "accept-all is still RISKY"


def test_an_error_body_without_data_is_not_checked(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({("email-verifier", "a@x.com"): {"errors": [{"id": "too_many_requests"}]}}))
    assert cf.verify("a@x.com")[0] == cf.NOT_CHECKED


def test_a_failed_domain_search_is_not_none_found(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({}))
    assert cf.find_for_domain("brand.com")["status"] == cf.NOT_CHECKED


def test_an_answered_empty_search_is_still_none_found(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({("domain-search", "brand.com"): {"data": {"emails": []}}}))
    assert cf.find_for_domain("brand.com")["status"] == cf.NONE_FOUND


def test_a_search_whose_verify_fails_records_nothing(monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({("domain-search", "brand.com"): _found("jo@brand.com")}))
    hit = cf.find_for_domain("brand.com")
    assert hit["status"] == cf.NOT_CHECKED and hit["email"] == ""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "Money").mkdir()
    tracker = tmp_path / "Money" / "prospect-tracker.csv"
    with open(tracker, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "domain", "wave"])
        w.writeheader()
        for b, d in (("Alpha", "alpha.com"), ("Beta", "beta.com"), ("Gamma", "gamma.com")):
            w.writerow({"brand": b, "domain": d, "wave": "9"})
    cf.init(str(tmp_path), runtime_fn=lambda: "local")
    monkeypatch.setenv("HUNTER_API_KEY", "test")
    yield tracker
    cf.init("", None)


def _rows(tracker):
    with open(tracker, newline="", encoding="utf-8") as f:
        return {r["brand"]: r for r in csv.DictReader(f)}


def test_fill_stops_at_the_first_unanswered_brand_and_leaves_it_searchable(vault, monkeypatch):
    fake = _fake({("domain-search", "alpha.com"): _found("jo@alpha.com"),
                  ("email-verifier", "jo@alpha.com"): _verdict("deliverable", 96)})
    monkeypatch.setattr(cf, "_fetch", fake)
    msg = cf.fill_contacts(wave="9", limit=5)
    rows = _rows(vault)
    assert rows["Alpha"]["email"] == "jo@alpha.com" and rows["Alpha"]["email_status"] == cf.SENDABLE
    assert not rows["Beta"].get("email_checked") and not rows["Beta"].get("email_status"), \
        "an unanswered brand gets no checked date, so it is searched again next time"
    assert ("domain-search", "gamma.com") not in fake.calls, "no calls after Hunter stops answering"
    assert "Hunter gave no answer at Beta" in msg


def test_fill_with_no_answer_at_all_writes_nothing(vault, monkeypatch):
    monkeypatch.setattr(cf, "_fetch", _fake({}))
    before = vault.read_text()
    msg = cf.fill_contacts(wave="9", limit=5)
    assert vault.read_text() == before and "Hunter gave no answer at Alpha" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
