"""splitframe_queue.py reply: the answer to a founder's reply, drafted for Alex to send.
No network: Gmail, the schedule and the model are all stand-ins."""
import importlib.util
import os
import types
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load("splitframe_reply")
sq = _load("splitframe_queue")


def _ev(y, mo, d, h1, m1, h2, m2):
    return {"start": datetime(y, mo, d, h1, m1), "end": datetime(y, mo, d, h2, m2), "title": "x"}


def test_the_two_times_are_free_evenings_on_the_next_weekdays():
    friday_morning = datetime(2026, 9, 25, 8, 0, tzinfo=rp.LOCAL_TZ)
    schedule = {  # Monday: practice 18:30-20:30, so no evening start clears it by 15 min
        datetime(2026, 9, 28).date(): [_ev(2026, 9, 28, 18, 30, 20, 30)],
    }
    got = rp.free_slots(lambda d: schedule.get(d, []), friday_morning)
    assert got == [datetime(2026, 9, 28, 17, 30), datetime(2026, 9, 29, 19, 0)]


def test_no_weekend_and_nothing_inside_18_hours():
    sunday_night = datetime(2026, 9, 27, 23, 0, tzinfo=rp.LOCAL_TZ)
    got = rp.free_slots(lambda d: [], sunday_night)
    # 23:00 Sunday + 18 h = 17:00 Monday, so Monday's evening still counts; Saturday never does.
    assert got == [datetime(2026, 9, 28, 19, 0), datetime(2026, 9, 29, 19, 0)]
    friday_night = datetime(2026, 9, 25, 23, 0, tzinfo=rp.LOCAL_TZ)
    assert all(s.weekday() < 5 for s in rp.free_slots(lambda d: [], friday_night))


def test_a_booked_day_is_skipped_not_squeezed():
    busy = [_ev(2026, 9, 28, 11, 0, 21, 30)]
    got = rp.free_slots(lambda d: busy if d.day == 28 else [], datetime(2026, 9, 25, 8, 0))
    assert got[0].day == 29


def test_times_read_the_way_he_would_write_them():
    assert rp.fmt_slot(datetime(2026, 9, 29, 19, 30)) == "Tuesday 9/29 at 7:30 PM ET"
    assert rp.fmt_slot(datetime(2026, 9, 30, 20, 0)) == "Wednesday 9/30 at 8 PM ET"


def test_their_latest_skips_the_studio():
    msgs = [{"sender": "Alex <alexhickey@splitframestudio.com>", "messageTimestamp": "3"},
            {"sender": "Jake <jake@treejuice.com>", "messageTimestamp": "2", "messageText": "sure"},
            {"sender": "Jake <jake@treejuice.com>", "messageTimestamp": "1"}]
    assert rp.their_latest(msgs)["messageTimestamp"] == "2"
    assert rp.our_latest(msgs)["messageTimestamp"] == "3"
    assert rp.their_latest(msgs[:1]) == {}


def test_quoted_history_is_dropped():
    m = {"messageText": "How much is it?\n\nOn Tue, Alex wrote:\n> five of your ads"}
    assert rp.body_text(m) == "How much is it?"


def test_the_brief_is_found_by_its_title(tmp_path):
    for folder, title in (("burnd", "burnd"), ("tees-by-taylor", "TEES by taylor")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "precall-2026-09-25.md").write_text(
            f"# Splitframe Studio — Pre-Call Brief: {title}\n\nbody")
    path, text = rp.find_precall(str(tmp_path), "TEES by taylor")
    assert path.endswith("tees-by-taylor/precall-2026-09-25.md") and "body" in text
    assert rp.find_precall(str(tmp_path), "Nobody") == ("", "")


SLOTS = ["Monday 9/28 at 7 PM ET", "Tuesday 9/29 at 7 PM ET"]


INTERESTED = ("Thanks for writing back. If you want more like it, the first drop is $650. I build 15 "
              "ads around the angles your live ads aren't running, 12 statics and 3 short cuts. "
              "Within 72 hours, no contract.\n\nWant me to start with the Banana Maple?\n\nAlex")


def test_checks_follow_the_playbook():
    assert rp.check_reply("interested", INTERESTED, SLOTS) == []
    assert any("didn't ask for" in p for p in rp.check_reply(
        "interested", INTERESTED + f" Or {SLOTS[0]}?", SLOTS))
    assert any("outside the price story" in p for p in rp.check_reply(
        "pricing", INTERESTED.replace("$650", "$500"), SLOTS))
    assert any("a link" in p for p in rp.check_reply(
        "pricing", INTERESTED + " splitframestudio.com, https://splitframestudio.com", SLOTS))
    assert any('"AI"' in p for p in rp.check_reply("interested", INTERESTED + " AI", SLOTS))
    call = f"Sure. {SLOTS[0]} or {SLOTS[1]} works, or send me a time that works. Twenty minutes.\n\nAlex"
    assert rp.check_reply("call", call, SLOTS) == []
    assert any("word for word" in p for p in rp.check_reply("call", call.replace("7 PM", "7pm"), SLOTS))


def test_a_hostile_reply_gets_exactly_the_removal_line():
    assert rp.check_reply("hostile", "Understood, you're off my list. Sorry for the noise.", SLOTS) == []
    assert rp.check_reply("hostile", "Sorry! Removing you now, and good luck with the launch.", SLOTS)


def test_the_facts_come_from_the_row():
    assert "no, attach it now" in rp.row_facts({"close_variant": "arm-B", "email_status": "published"})
    assert "printed on their own site" in rp.row_facts({"email_status": "published"})
    assert "Hunter" in rp.row_facts({"email_status": "deliverable"})
    assert "do not guess" in rp.row_facts({})


def test_the_model_json_is_parsed_even_in_a_fence():
    assert rp.parse_reply('```json\n{"kind": "No", "body": "Fair enough."}\n```') == ("no", "Fair enough.")
    assert rp.parse_reply('{"kind": "no", "body": ') == ("", "")


class _Client:
    def __init__(self, texts):
        self.texts = list(texts)
        self.messages = self

    def create(self, **kw):
        t = self.texts.pop(0)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=t)])


def test_a_failed_generation_is_retried_and_a_bad_one_reported():
    good = ('{"kind": "no", "body": "Totally fair, thanks for saying so. If the fall push changes '
            'things, the offer stands and I can send the ad over any time.\\n\\nAlex"}')
    kind, body, problems = rp.write_reply(_Client(["not json", good]), "ask", SLOTS)
    assert kind == "no" and problems == [] and body.startswith("Totally fair")
    bad = ('{"kind": "pricing", "body": "Sure, it is $500 for the drop and $1,200 a month after that, '
           'details at https://splitframestudio.com if useful.\\n\\nAlex"}')
    kind, body, problems = rp.write_reply(_Client([bad, bad, bad]), "ask", SLOTS)
    assert problems and any("price story" in p for p in problems)


def test_a_reply_is_matched_to_its_row_by_address_then_domain():
    rows = [{"brand": "Tree Juice", "email": "jake@treejuice.com", "domain": "treejuice.com"},
            {"brand": "Snee", "email_generic": "hello@snee.fun", "domain": "www.snee.fun"},
            {"brand": "Gmailer", "email": "shop@gmail.com", "domain": "gmail.com"}]
    assert sq.reply_row(rows, "jake@treejuice.com")["brand"] == "Tree Juice"
    assert sq.reply_row(rows, "sam@snee.fun")["brand"] == "Snee"
    assert sq.reply_row(rows, "stranger@gmail.com") is None


def test_study_and_work_blocks_can_hold_a_call_class_and_gym_cannot():
    """His real Monday 9/28 grid: every half hour is planned, so every block used to count."""
    def ev(h1, m1, h2, m2, t):
        return {"start": datetime(2026, 9, 28, h1, m1), "end": datetime(2026, 9, 28, h2, m2), "title": t}
    monday = [ev(12, 30, 14, 0, "CSDS 101 · 12:35–1:50 · Sears 333"),
              ev(14, 30, 17, 0, "Gym · afternoon session 2:30–5:00"),
              ev(17, 0, 18, 30, "Back to dorm · clean up / work / study 5:10–6:45"),
              ev(18, 30, 19, 30, "Dinner 6:45–7:30"),
              ev(19, 30, 20, 0, "50/50 · 7:30–8:10"),
              ev(20, 0, 21, 0, "Study / work 8:10–8:50"),
              ev(21, 0, 21, 30, "Night routine 8:50–9:30")]
    assert rp.is_flexible(monday[2]) and rp.is_flexible(monday[5])
    assert not any(rp.is_flexible(b) for b in (monday[0], monday[1], monday[3], monday[4], monday[6]))
    got = rp.free_slots(lambda d: monday if d.day == 28 else [], datetime(2026, 9, 25, 8, 0))
    assert got[0] == datetime(2026, 9, 28, 17, 30)


# ---------------------------------------------------------------------------
# The Stripe payment links (2026-09-26). The day-0 email after a yes carries the first-drop link
# verbatim and the five brief questions; the retainer link goes out only on a "go" to the monthly
# line. Every other reply still carries no link, and nothing promises ACH (not on the links yet).
# ---------------------------------------------------------------------------

DAY0 = ("Great, let's do it.\n\n" + rp.FIRST_DROP_LINK + "\n\n" + rp.KICKOFF_LINE + "\n"
        + "\n".join(f"{i}. {q}" for i, q in enumerate(rp.BRIEF_QUESTIONS, 1)) + "\n\nAlex")
GO = ("Done. Here's the monthly.\n\n" + rp.RETAINER_LINK
      + "\n\n20 new ads a month, five a week, plus the readout. Cancel any month.\n\nAlex")


def test_the_day0_email_carries_the_first_drop_link_verbatim():
    assert rp.FIRST_DROP_LINK == "https://buy.stripe.com/aFaeVdgqD3iM1C724MeEo00"
    assert rp.check_reply("yes", DAY0, SLOTS) == []
    assert any("first-drop payment link" in p for p in rp.check_reply(
        "yes", DAY0.replace(rp.FIRST_DROP_LINK, "https://buy.stripe.com/aFaeVdgqD3iM1C724MeEo0"), SLOTS))
    assert any("word for word" in p for p in rp.check_reply(
        "yes", DAY0.replace(rp.BRIEF_QUESTIONS[2], "Anything off limits?"), SLOTS))
    assert any("72 hours" in p for p in rp.check_reply("yes", DAY0.replace(rp.KICKOFF_LINE, ""), SLOTS))
    curly = DAY0.replace("'", "’")
    assert rp.check_reply("yes", curly, SLOTS) == [], "a curly apostrophe is the same words"


def test_the_retainer_link_only_after_the_monthly_offer():
    assert rp.check_reply("go", GO, SLOTS, offered=True) == []
    assert any("no monthly offer" in p for p in rp.check_reply("go", GO, SLOTS, offered=False))
    assert any("a link" in p for p in rp.check_reply("go", GO, SLOTS, offered=False))
    assert any("a link" in p for p in rp.check_reply("yes", DAY0 + "\n" + rp.RETAINER_LINK, SLOTS))


def test_every_other_reply_still_carries_no_link():
    for kind in ("interested", "pricing", "not_now", "call", "other"):
        assert any("a link" in p for p in rp.check_reply(kind, INTERESTED + " " + rp.FIRST_DROP_LINK, SLOTS))


def test_nothing_promises_ach():
    assert any("ACH" in p for p in rp.check_reply("yes", DAY0 + "\nACH works too.", SLOTS))
    assert any("ACH" in p for p in rp.check_reply("pricing", INTERESTED + " Happy to take a bank transfer.", SLOTS))


def test_the_monthly_offer_is_read_from_our_emails_only():
    ours = {"sender": "Alex <alexhickey@splitframestudio.com>", "messageText":
            'If you want this every month, it\'s $950 for 20 new ads. Reply "go" and I\'ll send the Stripe payment link.'}
    theirs = {"sender": "Jake <jake@treejuice.com>", "messageText": 'Reply "go"? ok go'}
    assert rp.retainer_offered([theirs, ours]) is True
    assert rp.retainer_offered([theirs]) is False
    assert "monthly offer" in rp.thread_facts([ours]) and rp.thread_facts([ours]).rstrip().endswith("yes")


def test_the_voice_prompt_carries_both_links_and_no_placeholder():
    assert rp.FIRST_DROP_LINK in rp.REPLY_VOICE and rp.RETAINER_LINK in rp.REPLY_VOICE
    assert "{FIRST_DROP}" not in rp.REPLY_VOICE and "{RETAINER}" not in rp.REPLY_VOICE
    assert all(q in rp.REPLY_VOICE for q in rp.BRIEF_QUESTIONS)
