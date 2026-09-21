"""A first touch may not go out with a placeholder subject.

2026-09-20: five first touches sent with the subject line "your ad account" — Cape Candle, Dakota
Tallow, Final Boss Sour, Friday Pickleball, Geode Swimwear. Every other send that week carried a
real observation. The drafting worker passed a placeholder and the only validation was
`if not subject`, so empty was refused and generic sailed through.

The subject decides whether the email is opened at all. A vague one reads as exactly the blast this
pitch depends on not being, and it wastes both the prospect and the live ad-account read that
earned the right to write to them.

The gate is calibrated against real production subjects below: it must refuse the placeholder and
pass every genuine one. A gate that refuses good subjects is worse than none — a refused draft
stalls the queue.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.expanduser("~/second-brain/scripts"))
import splitframe_queue as q  # noqa: E402


# Every real first-touch/follow-up subject actually sent in the week of 2026-09-15.
REAL_SUBJECTS = [
    "The NY Mag quote is buried in one ad out of nine",
    "Sold out 6x, unchanged since March",
    "The skin cancer story ad is two and a half years old",
    "Fourteen ads, one sentence",
    "Ten cuts of the same fifty five words",
    "the pull that went sideways today",
    "your nick clip already has 1.3k views on its own",
    "Same script, five different lengths",
    "The founder story is one ad out of nine",
    "Seven of your ten ads are the same post",
    "Two scripts are carrying a 32-ad account",
    "Backend text leaked into your newest ad",
    "Four of your five ads are the same three lines",
    "A third of your ads run the same urgency script",
    "Half your account is one ad, recut ten ways",
    "One sale ad has said \"a few hours left\" since February",
    "One ad still says 20,000. The rest say 90,000",
]


@pytest.mark.parametrize("subject", REAL_SUBJECTS)
def test_every_real_subject_passes(subject):
    # False positives stall the queue, so this is the half of the contract that matters most.
    assert q.subject_problem(subject) == "", subject


def test_the_exact_placeholder_that_shipped_is_refused():
    problem = q.subject_problem("your ad account")
    assert problem
    assert "placeholder" in problem


@pytest.mark.parametrize("subject", [
    "Your Ad Account",          # casing must not launder it
    "your ad account.",         # nor punctuation
    "  your   ad   account  ",  # nor spacing
    "your ads",
    "quick question",
    "Following up",
])
def test_generic_subjects_are_refused(subject):
    assert q.subject_problem(subject)


def test_an_empty_subject_is_still_refused():
    assert q.subject_problem("") == "no subject"
    assert q.subject_problem(None) == "no subject"


def test_a_too_short_subject_is_refused():
    problem = q.subject_problem("Nine ads")
    assert problem and "vague" in problem


def test_the_shortest_real_subject_still_passes():
    # "Fourteen ads, one sentence" is the floor the word minimum was set against; if someone
    # raises MIN_SUBJECT_WORDS this fails rather than silently refusing good work.
    assert q.subject_problem("Fourteen ads, one sentence") == ""


def test_the_gate_is_wired_into_the_queue_path():
    # The function existing is not the fix; plan_add calling it is.
    import inspect
    assert "subject_problem" in inspect.getsource(q.plan_add)
