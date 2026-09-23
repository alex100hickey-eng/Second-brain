"""Bounce detection. A delivery failure is invisible to every reply watcher by construction —
it comes from mailer-daemon at one of OUR domains, not a prospect's — and it is the number that
says whether the sending domain is safe and whether the daily cap can go up."""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ad_creative_pipeline as acp                            # noqa: E402

COLS = ["brand", "domain", "email", "email_generic", "sent_date", "replied", "outcome"]
ROWS = [
    {"brand": "Obvi", "domain": "myobvi.com", "email": "ankit@myobvi.com",
     "email_generic": "", "sent_date": "2026-09-17", "replied": "", "outcome": ""},
    {"brand": "Calypsa", "domain": "calypsa.com", "email": "",
     "email_generic": "hello@calypsa.com", "sent_date": "2026-09-16", "replied": "", "outcome": ""},
    {"brand": "Answered", "domain": "answered.com", "email": "a@answered.com",
     "email_generic": "", "sent_date": "2026-09-10", "replied": "2026-09-11", "outcome": ""},
    {"brand": "NeverSent", "domain": "never.com", "email": "n@never.com",
     "email_generic": "", "sent_date": "", "replied": "", "outcome": ""},
]


def _tracker(tmp):
    path = os.path.join(tmp, "prospect-tracker.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(ROWS)
    acp._tracker_path = lambda: path
    return path


def _bounce(to, mid="m1", sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>"):
    return {"id": mid, "sender": sender,
            "subject": "Delivery Status Notification (Failure)",
            "preview": {"body": f"Your message wasn't delivered to {to} because the address "
                                "couldn't be found, or is unable to receive mail."}}


def test_finds_a_bounce_for_an_address_we_wrote_to():
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        hits = acp.detect_bounces(lambda q: [_bounce("ankit@myobvi.com")])
        assert hits == [{"address": "ankit@myobvi.com", "brand": "Obvi", "id": "m1",
                         "subject": "Delivery Status Notification (Failure)"}]


def test_finds_one_for_a_front_desk_address_too():
    """Those live in email_generic, and they are the newest and least proven addresses in the
    tracker — exactly the ones a bounce check exists for."""
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        hits = acp.detect_bounces(lambda q: [_bounce("hello@calypsa.com")])
        assert hits and hits[0]["brand"] == "Calypsa"


def test_ignores_what_is_not_ours():
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        # An address the tracker never sent to: someone else's bounce is not our rate.
        assert acp.detect_bounces(lambda q: [_bounce("stranger@elsewhere.com")]) == []
        # Never sent to at all, so a failure says nothing about our sending.
        assert acp.detect_bounces(lambda q: [_bounce("n@never.com")]) == []
        # Ordinary mail that happens to quote an address is not a delivery failure.
        assert acp.detect_bounces(lambda q: [
            {"id": "m9", "sender": "someone@myobvi.com", "subject": "re: your note",
             "preview": {"body": "forwarding to ankit@myobvi.com"}}]) == []
        # Already counted once.
        assert acp.detect_bounces(lambda q: [_bounce("ankit@myobvi.com")], seen={"m1"}) == []


def test_a_reply_does_not_exempt_a_later_bounce():
    """sent_domains drops brands that answered; sent_addresses must not — a brand replying in
    September says nothing about whether October's mail reached them."""
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        assert "a@answered.com" in acp.sent_addresses()
        assert acp.detect_bounces(lambda q: [_bounce("a@answered.com")])[0]["brand"] == "Answered"


def test_rate_denominator_is_recent_sends_only():
    """A rate against all-time sends keeps looking fine while today's sending burns."""
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        assert acp.sent_since("2026-09-16") == 2
        assert acp.sent_since("2026-09-01") == 3
        assert acp.sent_since("2026-12-01") == 0


def test_fetch_failure_is_not_a_clean_bill_of_health_crash():
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)

        def boom(_q):
            raise RuntimeError("gmail down")

        assert acp.detect_bounces(boom) == []


# ---- the creator lane has no tracker row (deliverability audit, 2026-09-23) ----

CREATOR_LIST = """# Creator lane — prospect list

### MISTERARTHER — the best fit found so far
- **Email:** `contact@misterarther.com` — **VERIFIED 2026-09-17.**

### Mason
- **Email:** `masondota2@afkcreators.com` — read directly off his own Twitch About panel FAQ
"""

MASON_DSN = {"id": "dsn1", "sender": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
             "subject": "Delivery Status Notification (Failure)",
             "messageText": "** Address not found **\n\nYour message wasn't delivered to "
                            "masondota2@afkcreators.com because the address couldn't be found, "
                            "or is unable to receive mail.\n\n550 5.1.1 The email account that "
                            "you tried to reach does not exist."}


def _creators(tmp, monkeypatch, text=CREATOR_LIST):
    path = os.path.join(tmp, "Creator Lane — Prospects.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    monkeypatch.setattr(acp, "_creator_list_path", lambda: path)
    return path


def test_the_creator_list_is_parsed_into_addresses_with_names():
    got = acp.creator_addresses(CREATOR_LIST)
    assert got == {"contact@misterarther.com": "MISTERARTHER",
                   "masondota2@afkcreators.com": "Mason"}


def test_a_creator_bounce_is_found_with_the_real_dsn_text(monkeypatch):
    """The 2026-09-22 bounce, verbatim. It was discarded because the tracker had never heard of
    the address, and splitframe:bounces stayed empty while the cap ramp climbed."""
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        _creators(tmp, monkeypatch)
        hits = acp.detect_bounces(lambda q: [MASON_DSN])
        assert hits == [{"address": "masondota2@afkcreators.com", "brand": "Mason", "id": "dsn1",
                         "subject": "Delivery Status Notification (Failure)"}]


def test_no_creator_list_changes_nothing_for_the_tracker(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _tracker(tmp)
        monkeypatch.setattr(acp, "_creator_list_path", lambda: None)
        assert acp.detect_bounces(lambda q: [MASON_DSN]) == []
        assert acp.detect_bounces(lambda q: [_bounce("ankit@myobvi.com")])[0]["brand"] == "Obvi"


def test_creator_sends_count_in_the_rate_denominator():
    creators = acp.creator_addresses(CREATOR_LIST)
    queue = {"queue": [
        {"to": "contact@misterarther.com", "released": "2026-09-19T12:30:00-04:00"},
        {"to": "masondota2@afkcreators.com", "released": "2026-09-22T00:56:00-04:00"},
        {"to": "info@antlerfarms.com", "released": "2026-09-19T12:30:00-04:00"},   # tracker lane
        {"to": "zerbs@evolved.gg", "released": ""},                                  # still queued
    ]}
    assert acp.creator_sent_since("2026-09-19", queue, creators) == 2
    assert acp.creator_sent_since("2026-09-20", queue, creators) == 1
    assert acp.creator_sent_since("2026-09-19", {}, creators) == 0
    assert acp.creator_sent_since("2026-09-19", queue, {}) == 0
