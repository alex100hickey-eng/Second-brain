"""The follow-up audit (Money/Research — follow-up audit, 2026-09-25): a body that is machine output
never reaches a founder, a follow-up never offers work that doesn't exist, touch 3 lands on day 10.
No network."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sfd = _load("splitframe_daily")
send = _load("splitframe_send")

GOOD_T2 = ("Natasha,\n\nDifferent idea from my first email.\n\n\"Made to be worn, not saved\" is the "
           "best line on your About page. I'd test it as a plain static against the sale carousel.\n\n"
           "When someone buys a second suit, is it a different cut or the same one in a new colour?"
           "\n\nAlex Hickey\nSplitframe Studio")


def test_the_two_bodies_that_went_out_as_json_are_caught():
    # the real openings, 2026-09-20 Monday Swimwear and 2026-09-21 Goodwipes
    assert sfd.broken_body('{"body":"Natasha — six of your ads') == "starts with raw code"
    assert sfd.broken_body('Here you go:\n{"body": "Charlie,') == 'contains a "body": key'
    assert sfd.broken_body("```json\n{}") == "starts with raw code"
    assert sfd.broken_body(GOOD_T2) == ""


def test_a_follow_up_may_not_offer_work_that_does_not_exist():
    offer = GOOD_T2.replace("I'd test it", "I'll build it free, yours either way, and test it")
    assert "offers to build something that doesn't exist" in sfd.followup_problems(offer, static_sent=False)
    t3_after_static = ("Mark,\n\nLast one from me. The Papa Candle ad is yours to run whether we ever "
                       "talk or not.\n\nIf ads aren't a priority right now, that's a fair no. If it's "
                       "timing, tell me a month and I'll check back then.\n\nIs it a no, or a not now?"
                       "\n\nAlex Hickey\nSplitframe Studio")
    assert sfd.followup_problems(t3_after_static, static_sent=True) == []
    assert sfd.followup_problems(GOOD_T2, static_sent=False) == []


def test_the_regrade_and_the_missing_question_are_refused():
    assert '"the math" re-grades the first email' in sfd.followup_problems(
        GOOD_T2.replace("Different idea", "Did the math again. Different idea"), False)
    no_q = GOOD_T2.replace("new colour?", "new colour.")
    assert "does not end on a question" in sfd.followup_problems(no_q, False)


def test_touch_three_is_day_ten():
    assert (sfd.FU1_DAYS, sfd.FU2_DAYS) == (3, 10)
    assert "Touch 3" in sfd.AD_VOICE and "day 7" not in sfd.AD_VOICE
    assert "concrete, honest offer" not in sfd.AD_VOICE, "the old prompt told it to offer free work"


def test_the_sender_holds_a_broken_body():
    item = {"detail": 'Subject: Re: your ad account\n\n{"body":"Natasha — six of'}
    assert send.body_is_broken(item) == "starts with raw code"
    assert send.body_is_broken({"detail": "Subject: Re: x\n\n" + GOOD_T2}) == ""


def test_a_sent_first_touch_is_stamped_for_day_3_and_day_10(tmp_path, monkeypatch):
    from datetime import date, timedelta
    t = tmp_path / "t.csv"
    t.write_text("brand,email,email_generic,sent_date,followup1_date,followup2_date\n"
                 "Tower 28,amy@tower28beauty.com,,,,\n", encoding="utf-8")
    monkeypatch.setattr(send, "TRACKER", str(t))
    send.stamp_tracker("amy@tower28beauty.com")
    _b, _e, _g, sent, fu1, fu2 = t.read_text().splitlines()[1].split(",")
    today = date.today()
    assert (sent, fu1, fu2) == (today.isoformat(), (today + timedelta(days=3)).isoformat(),
                                (today + timedelta(days=10)).isoformat())


def test_a_creator_row_keeps_day_7(tmp_path, monkeypatch):
    from datetime import date, timedelta
    t = tmp_path / "t.csv"
    t.write_text("brand,category,email,email_generic,sent_date,followup1_date,followup2_date\n"
                 "Guzu,creator,guzubusiness@hotmail.com,,,,\n", encoding="utf-8")
    monkeypatch.setattr(send, "TRACKER", str(t))
    send.stamp_tracker("guzubusiness@hotmail.com")
    assert t.read_text().splitlines()[1].split(",")[-1] == (date.today() + timedelta(days=7)).isoformat()
