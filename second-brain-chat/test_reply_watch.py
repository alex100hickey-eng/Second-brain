"""Reply watcher: telling a real human reply from a helpdesk autoresponder.

Why this file exists: on 2026-09-19 Calypsa's Gorgias autoresponder was stamped `replied` in the
tracker. due_followups() skips any row with `replied` set, so that one automated "we got your
ticket" silently retired a qualified, live prospect after a single touch — no follow-up could ever
be drafted again, and nothing in any log said so. The email had announced itself with a standard
`Auto-Submitted: auto-replied` header the watcher never read.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.expanduser("~/second-brain/scripts"))
import reply_watch as rw  # noqa: E402


def msg(subject="Backend text leaked into your newest ad", body="", headers=None):
    return {
        "subject": subject,
        "messageText": body,
        "payload": {"headers": [{"name": k, "value": v} for k, v in (headers or {}).items()]},
    }


# The exact message that caused the bug, headers as Gmail returned them.
CALYPSA = msg(
    body=("Hey there Alex,\n\nThanks for your note. We've received your request and we'll be "
          "responding as quickly as we can. We usually respond within a few hours :)\n\n"
          "Speak soon!\nThe Calypsa Team"),
    headers={"From": "Calypsa Support <hello@calypsa.com>", "Auto-Submitted": "auto-replied",
             "X-GORGIAS-TICKET-ID": "285818419"},
)

# What a real founder answering the closing question looks like.
HUMAN = msg(
    body=("Hi Alex, thanks for flagging that — the customizer text is embarrassing, we'll pull it "
          "today. The size push is mostly new customers. What would a test cost?"),
    headers={"From": "Nava <nava@calypsa.com>"},
)


def test_the_calypsa_autoresponder_is_recognised():
    assert rw.auto_reply_reason(CALYPSA)


def test_a_real_founder_reply_is_not_called_automatic():
    # The expensive direction: calling a human automatic hides a buying signal AND, because the row
    # never gets stamped, keeps chasing someone who already answered.
    assert rw.auto_reply_reason(HUMAN) == ""


def test_the_header_is_enough_on_its_own():
    # No autoresponder wording at all: the header must carry the decision.
    bare = msg(body="Got it.", headers={"Auto-Submitted": "auto-generated"})
    assert rw.auto_reply_reason(bare)


def test_auto_submitted_no_means_a_human_sent_it():
    # RFC 3834: "no" is the explicit human marker, not just an absent header.
    assert rw.auto_reply_reason(msg(body="Sounds good.", headers={"Auto-Submitted": "no"})) == ""


@pytest.mark.parametrize("subject", [
    "Automatic reply: Backend text leaked into your newest ad",
    "Out of Office",
    "Auto: your enquiry",
])
def test_subject_marks_an_autoresponder_when_the_header_is_missing(subject):
    assert rw.auto_reply_reason(msg(subject=subject, body="Back Monday."))


@pytest.mark.parametrize("headers", [
    {"Precedence": "bulk"},
    {"X-Autoreply": "yes"},
    {"X-Autorespond": "1"},
])
def test_the_other_standard_automation_markers_count(headers):
    assert rw.auto_reply_reason(msg(body="Thanks for reaching out.", headers=headers))


def test_our_own_quoted_email_cannot_trigger_detection():
    # Every reply quotes our outbound copy underneath. If body matching read the quoted block, our
    # own wording would decide whether their reply counts as human — and one unlucky phrase in a
    # template would silently retire every prospect it was sent to.
    quoted = msg(body=(
        "Yes, let's talk. Tuesday work?\n\n"
        "On Sat, Sep 19 2026, at 04:36 PM, Alex Hickey <alexhickey@splitframestudio.com> wrote:\n"
        "> This is an automated teardown of your account.\n"
        "> We usually respond within a day.\n"))
    assert rw.auto_reply_reason(quoted) == ""
    assert "Tuesday" in rw.body_text(quoted)
    assert "automated teardown" not in rw.body_text(quoted)


def test_a_human_saying_thanks_is_still_a_human():
    # Guard against loosening AUTO_BODY into generic politeness: "thanks", "received", "get back to
    # you" all appear in genuine replies.
    warm = msg(body="Thanks for reaching out! Received — I'll get back to you with numbers.")
    assert rw.auto_reply_reason(warm) == ""


def test_headers_survive_a_message_with_no_payload():
    assert rw.headers_of({}) == {}
    assert rw.auto_reply_reason({"subject": "hi", "messageText": "hi"}) == ""


def test_body_text_falls_back_to_the_preview_dict():
    # GMAIL_FETCH_EMAILS returns preview as a dict on some calls and a string on others.
    assert "ticket" in rw.body_text({"preview": {"body": "your ticket has been opened"}})
    assert "ticket" in rw.body_text({"preview": "your ticket has been opened"})


def test_an_autoresponder_without_full_text_is_still_caught_from_the_preview():
    from_preview = {"subject": "Re: ad", "preview": {"body": "This is an automated response."},
                    "payload": {"headers": []}}
    assert rw.auto_reply_reason(from_preview)


# ---------------------------------------------------------------------------
# Who the watcher can see at all.
#
# Same blind spot as the send gate, the tracker stamper and the daily cap: the drafter moved the
# funnel's addresses into `email_generic` and each consumer kept reading `email`. Here it meant a
# reply from an already-emailed brand hit no match and was dropped without a line in the log.
# ---------------------------------------------------------------------------

ROWS = [
    # front desk: address only in email_generic, and it doesn't match the `domain` column
    {"brand": "Universal Standard", "domain": "universalstandard.com", "email": "",
     "email_generic": "info@universalstandard.net"},
    # a brand that mails from a completely unrelated domain
    {"brand": "Fly By Jing", "domain": "flybyjing.com", "email": "",
     "email_generic": "flybyjing@isetta.co"},
    # reachable only at a shared mailbox
    {"brand": "Faded Floral Boutique", "domain": "", "email": "",
     "email_generic": "fadedfloralboutique@gmail.com"},
    {"brand": "Gunner Kennels", "domain": "gunner.com", "email": "nate@gunner.com",
     "email_generic": ""},
]


def test_a_front_desk_brand_is_watched_on_its_real_sending_domain():
    d = rw.prospect_domains(ROWS)
    assert d.get("universalstandard.net") == "Universal Standard"
    assert d.get("isetta.co") == "Fly By Jing"


def test_the_domain_and_email_columns_still_work():
    d = rw.prospect_domains(ROWS)
    assert d.get("gunner.com") == "Gunner Kennels"
    assert d.get("universalstandard.com") == "Universal Standard"


def test_a_shared_mailbox_never_claims_the_whole_provider():
    # If gmail.com landed in the domain map, every personal mail would read as a prospect reply.
    assert "gmail.com" not in rw.prospect_domains(ROWS)


def test_a_prospect_at_a_shared_mailbox_is_still_reachable():
    # ...but they must not be invisible either: gmail.com is in OWN, so before prospect_addresses
    # existed this brand could never be detected at all.
    assert rw.prospect_addresses(ROWS) == {"fadedfloralboutique@gmail.com": "Faded Floral Boutique"}


def test_every_emailed_brand_in_the_real_tracker_is_watchable():
    """The contract that actually matters: nobody we have emailed can be invisible."""
    rows = rw.tracker_rows()
    domains, addresses = rw.prospect_domains(rows), rw.prospect_addresses(rows)
    blind = []
    for r in rows:
        if not (r.get("sent_date") or "").strip():
            continue
        for col in ("email", "email_generic"):
            addr = (r.get(col) or "").strip().lower()
            if "@" not in addr:
                continue
            if addr in addresses or domains.get(addr.split("@", 1)[1]):
                break
        else:
            blind.append(r.get("brand"))
    assert not blind, f"emailed but invisible to the reply watcher: {blind}"


# ---------------------------------------------------------------------------
# The watchdog.
#
# 2026-09-21: the job reported perfect health — loaded, live PID, exit status 0 — and had not run
# for 34 hours. A run started Sunday 03:00 blocked on a network call during an overnight DNS wobble
# and never returned. launchd does not start a new instance while the previous one is alive, so ONE
# hung run silently disabled the job forever. Strictly worse than the disabled plist found on 09-19,
# because every external signal said it was fine.
#
# The Composio client accepts no timeout argument, so a per-call timeout cannot cover this.
# ---------------------------------------------------------------------------

def test_the_run_budget_fits_inside_the_launchd_interval():
    # The whole point: the process must be dead before launchd tries to start the next one, or the
    # hang compounds instead of clearing. StartInterval is 1800s.
    assert rw.RUN_BUDGET_SECONDS < 1800


def test_a_socket_timeout_is_set_so_nothing_blocks_forever():
    import socket
    assert socket.getdefaulttimeout() is not None


def test_arming_records_the_budget_it_actually_used():
    # The abort line reports this number. A diagnostic that lies about its own numbers is worse
    # than no diagnostic — it was printing the default constant regardless of what was armed.
    import signal
    rw.arm_watchdog(123)
    try:
        assert rw._armed_for == 123
    finally:
        signal.alarm(0)
        rw.arm_watchdog(rw.RUN_BUDGET_SECONDS)
        signal.alarm(0)


def test_main_arms_the_watchdog_before_doing_any_work():
    # Arming after the network call would protect nothing; the import of Composio and the fetch
    # both have to be inside the alarm.
    import inspect
    body = inspect.getsource(rw.main)
    first = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith(("def", '"""'))][0]
    assert first == "arm_watchdog()", first


# ---------------------------------------------------------------------------
# Replies from an address the tracker doesn't know, on a thread we sent into.
#
# A press@ or info@ desk forwards to an agency or to the founder's own gmail, and they answer on
# our thread from there. Matching by sender domain never saw those. The thread is the one thing
# they can't change.
# ---------------------------------------------------------------------------

def _sent(to, labels=("SENT",)):
    return {"labelIds": list(labels), "to": to, "sender": "alexhickey@splitframestudio.com"}


def test_the_thread_names_who_we_wrote_to():
    assert rw.thread_recipient([_sent("press@moonjuice.com")]) == "press@moonjuice.com"
    assert rw.thread_recipient([_sent("Jo <jo@x.com>, cc@y.com")]) == "jo@x.com"


def test_an_unsent_draft_in_the_thread_does_not_count():
    assert rw.thread_recipient([_sent("press@moonjuice.com", labels=("DRAFT",))]) == ""
    assert rw.thread_recipient([]) == ""


@pytest.mark.parametrize("addr,worth", [
    ("jane@agency.com", True), ("founder@gmail.com", True),
    ("mailer-daemon@googlemail.com", False), ("postmaster@outlook.com", False),
    ("alexhickey@splitframestudio.com", False), ("noreply-dmarc-support@google.com", False),
    ("contact@mail.hunter.io", False), ("noreply@accounts.google.com", False),
    ("james@hunter.io", False), ("", False)])
def test_who_is_worth_a_thread_check(addr, worth):
    assert rw.worth_a_thread_check(addr) is worth


class _FakeComposio:
    def __init__(self, inbox, threads):
        self.inbox, self.threads, self.thread_calls = inbox, threads, []
        self.tools = self

    def execute(self, slug, user_id=None, dangerously_skip_version_check=None, arguments=None):
        if slug == "GMAIL_FETCH_EMAILS":
            assert arguments.get("include_spam_trash"), "spam must be read too"
            return {"successful": True, "data": {"messages": self.inbox}}
        if slug == "GMAIL_FETCH_MESSAGE_BY_THREAD_ID":
            self.thread_calls.append(arguments["thread_id"])
            return {"successful": True, "data": {"messages": self.threads.get(arguments["thread_id"], [])}}
        raise AssertionError(slug)


def _inbound(mid, sender, thread, subject="Re: Half your account is one ad", body="", headers=None, labels=("INBOX",)):
    m = msg(subject=subject, body=body, headers=headers)
    m.update({"messageId": mid, "threadId": thread, "sender": sender, "labelIds": list(labels)})
    return m


@pytest.fixture
def watch(monkeypatch):
    import types
    rows = [{"brand": "Moon Juice", "domain": "moonjuice.com", "email": "",
             "email_generic": "press@moonjuice.com", "sent_date": "2026-09-19"}]
    state, logged, stamped, nudged = {"seen": []}, [], [], []
    monkeypatch.setattr(rw, "tracker_rows", lambda: rows)
    monkeypatch.setattr(rw, "load_state", lambda: json.loads(json.dumps(state)))
    monkeypatch.setattr(rw, "save_state", lambda st: state.update(st))
    monkeypatch.setattr(rw, "log", logged.append)
    monkeypatch.setattr(rw, "stamp_replied", lambda b, w: stamped.append(b))
    monkeypatch.setattr(rw, "nudge", lambda *a, **k: nudged.append(a[0]))
    monkeypatch.setattr(rw, "_refresh_funnel", lambda: None)
    monkeypatch.setattr(rw, "_beat", lambda note="": None)
    monkeypatch.setattr(rw, "arm_watchdog", lambda seconds=None: None)
    monkeypatch.setenv("COMPOSIO_API_KEY", "test")

    def go(inbox, threads):
        fake = _FakeComposio(inbox, threads)
        monkeypatch.setitem(sys.modules, "composio", types.SimpleNamespace(Composio=lambda api_key: fake))
        assert rw.main() == 0
        return fake
    go.logged, go.stamped, go.nudged, go.state = logged, stamped, nudged, state
    return go


def test_an_agency_reply_on_our_thread_is_a_reply(watch):
    inbox = [_inbound("m1", "Jane Doe <jane@pr-agency.com>", "T1",
                      body="Hi Alex, Amanda forwarded this. Can you send rates?")]
    fake = watch(inbox, {"T1": [_sent("press@moonjuice.com")]})
    assert watch.stamped == ["Moon Juice"] and watch.nudged == ["Moon Juice replied"]
    assert any("REPLY from Moon Juice <jane@pr-agency.com> (on our thread to press@moonjuice.com)" in l
               for l in watch.logged)
    assert fake.thread_calls == ["T1"]


def test_a_reply_filed_in_spam_is_still_a_reply(watch):
    inbox = [_inbound("m1", "Amanda <amanda@moonjuice.com>", "T1", body="yes, let's talk", labels=("SPAM",))]
    watch(inbox, {})
    assert watch.stamped == ["Moon Juice"]


def test_a_bounce_on_our_thread_is_not_a_reply(watch):
    inbox = [_inbound("m1", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "T1",
                      subject="Delivery Status Notification (Failure)")]
    fake = watch(inbox, {"T1": [_sent("press@moonjuice.com")]})
    assert watch.stamped == [] and fake.thread_calls == [], "bounces are never even looked up"


def test_an_agency_autoresponder_on_our_thread_leaves_follow_ups_alive(watch):
    inbox = [_inbound("m1", "Desk <desk@pr-agency.com>", "T1", subject="Automatic reply: Re: Half your account",
                      headers={"Auto-Submitted": "auto-replied"})]
    watch(inbox, {"T1": [_sent("press@moonjuice.com")]})
    assert watch.stamped == []
    assert any("AUTO-REPLY from Moon Juice" in l for l in watch.logged)


def test_a_stranger_off_our_threads_costs_one_lookup_ever(watch):
    inbox = [_inbound("m9", "Someone <someone@elsewhere.com>", "T9", subject="partnership?")]
    fake = watch(inbox, {"T9": []})
    assert watch.stamped == [] and fake.thread_calls == ["T9"]
    fake2 = watch(inbox, {"T9": []})
    assert fake2.thread_calls == [], "the answer is cached in state"


# ---------------------------------------------------------------------------
# A dropped connection is retried, and a scan that never happened says so in reply_watch.log.
# ---------------------------------------------------------------------------

class _Flaky(_FakeComposio):
    def __init__(self, fails, inbox=()):
        super().__init__(list(inbox), {})
        self.fails, self.fetches = fails, 0

    def execute(self, slug, **kw):
        if slug == "GMAIL_FETCH_EMAILS":
            self.fetches += 1
            if self.fetches <= self.fails:
                raise ConnectionError("Connection error.")
        return super().execute(slug, **kw)


def _run_with(monkeypatch, watch, fake):
    import types
    monkeypatch.setattr(rw.time, "sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "composio", types.SimpleNamespace(Composio=lambda api_key: fake))
    return rw.main()


def test_a_dropped_connection_is_retried(watch, monkeypatch):
    fake = _Flaky(fails=2)
    assert _run_with(monkeypatch, watch, fake) == 0
    assert fake.fetches == 3 and any("no prospect replies" in l for l in watch.logged)


def test_a_scan_that_never_happened_is_logged_not_silent(watch, monkeypatch):
    fake = _Flaky(fails=99)
    assert _run_with(monkeypatch, watch, fake) == 1
    assert any("inbox read FAILED 3 times" in l for l in watch.logged)
    assert not any("no prospect replies" in l for l in watch.logged), "a failed read is not a quiet inbox"
