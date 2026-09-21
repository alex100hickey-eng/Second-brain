"""Reply watcher: telling a real human reply from a helpdesk autoresponder.

Why this file exists: on 2026-09-19 Calypsa's Gorgias autoresponder was stamped `replied` in the
tracker. due_followups() skips any row with `replied` set, so that one automated "we got your
ticket" silently retired a qualified, live prospect after a single touch — no follow-up could ever
be drafted again, and nothing in any log said so. The email had announced itself with a standard
`Auto-Submitted: auto-replied` header the watcher never read.
"""
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
