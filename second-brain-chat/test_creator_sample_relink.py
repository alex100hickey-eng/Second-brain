"""Creator follow-ups re-link the sample clip the first touch linked, and never offer a new one.
The creator first touches are sample-first (a clip already cut from their own stream); a touch 2
that forgets the link, or offers to "cut one free", undoes the whole pitch. No network."""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # this checkout
spec = importlib.util.spec_from_file_location("splitframe_daily", os.path.join(ROOT, "scripts", "splitframe_daily.py"))
sfd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sfd)

URL = "https://splitframestudio.com/samples/guzu-3a9f01bc/"


def test_the_url_comes_from_the_first_email_itself(tmp_path):
    first = f"Guzu,\n\nCut your clutch into a 40 second vertical. It's here: {URL}\n\nAlex"
    assert sfd.creator_sample_url({"domain": "twitch.tv/guzu"}, first, str(tmp_path / "none.json")) == URL


def test_the_sample_map_is_the_fallback(tmp_path):
    links = tmp_path / "sample-links.json"
    links.write_text(json.dumps({"guzu": {"url": URL}}))
    assert sfd.creator_sample_url({"domain": "twitch.tv/guzu"}, "no link in this one", str(links)) == URL
    assert sfd.creator_sample_url({"domain": "twitch.tv/nobody"}, "", str(links)) == ""


def test_the_creator_voice_relinks_and_never_offers():
    v = sfd.CREATOR_VOICE
    assert "The clip is still here:" in v and "yours to post" in v
    assert "static" not in v.split("Return STRICT JSON")[0].split("HARD CONSTRAINT")[-1], \
        "the ad-creative templates must not leak into the creator lane"
    assert "$400/mo" in v
    assert "Touch 2" in sfd.AD_VOICE and "as a static" in sfd.AD_VOICE, "the ad lane keeps its own"


def test_a_follow_up_that_offers_a_new_clip_is_refused():
    offer = (f"Guzu,\n\nThe clip is still here: {URL}\n\nI'll make you another one free this week.\n\n"
             "Which game pulls your biggest chat?\n\nAlex Hickey\nSplitframe Studio")
    assert "offers to build something that doesn't exist" in sfd.followup_problems(offer, static_sent=False)
    good = (f"Guzu,\n\nThe clip is still here: {URL}\n\nThe chat going silent before the clutch is the "
            "part a scroller stops for.\n\nWhich game pulls your biggest chat?\n\nAlex Hickey\nSplitframe Studio")
    assert sfd.followup_problems(good, static_sent=False) == []
