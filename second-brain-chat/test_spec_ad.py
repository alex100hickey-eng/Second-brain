"""The guards on a spec ad. It goes out unsolicited, under Alex's name, and is the first work
a brand ever sees from him — so what it may say is worth pinning."""
import importlib.util
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "spec_ad.py")
_spec = importlib.util.spec_from_file_location("spec_ad", PATH)
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)

GOOD = dict(headline="Swim you can actually swim in", body="Full coverage. Made to move.",
            cta="Shop the collection", allow=[])


def test_good_copy_passes():
    assert sa.copy_problems(**GOOD) == []


def test_refuses_claims_nobody_can_back():
    """Every one of these is a claim someone has to stand behind, and on a spec ad for a brand
    nobody has spoken to yet, nobody can."""
    for text, expect in [
        ("40% off this week", "a percentage"),
        ("The #1 swimsuit for real swimmers", '"#1"'),
        ("Clinically tested for sensitive skin", "a medical claim"),
        ("Loved by 12,000 reviews", "a review count"),
        ("Guaranteed to last all season", "an absolute claim"),
        ("Now $49 for a limited time", "a price"),
    ]:
        problems = sa.copy_problems(headline=text, body="", cta="", allow=[])
        assert any(expect in p for p in problems), f"{text!r} slipped through"


def test_a_claim_read_on_their_own_site_can_be_allowed():
    """The guard exists to stop invention, not to stop a brand's own published claim from
    appearing in an ad about that brand."""
    assert sa.copy_problems(headline="UPF 50+, 40% off through Sunday", body="", cta="",
                            allow=["a percentage"]) == []


def test_refuses_copy_the_feed_would_cut_off():
    long_head = "A headline that simply keeps going and going well past anything a phone screen "
    assert any("truncates" in p for p in
               sa.copy_problems(headline=long_head, body="", cta="", allow=[]))
    assert any("no headline" in p for p in sa.copy_problems(headline="", body="", cta="", allow=[]))
    assert any("nobody reads it" in p for p in
               sa.copy_problems(headline="Fine", body="x" * 200, cta="", allow=[]))


def test_shared_fabrication_guard_is_the_email_one():
    """Imported from splitframe_daily, not copied — two copies drift, and this one decides
    whether a stranger receives a claim about their own product that nobody checked."""
    assert sa.fabrication_risk is not None
    src = open(PATH).read()
    assert "def fabrication_risk" not in src


def test_html_escapes_copy():
    page = sa.build_html("A & B", "<script>alert(1)</script>", "", "", __file__, "light",
                         (1080, 1350))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "A &amp; B" in page


def test_crop_defaults_to_top():
    """"center" is the one crop that reliably cuts a model's head off; it did exactly that on
    the first ad this rendered."""
    page = sa.build_html("B", "H", "", "", __file__, "light", (1080, 1350))
    assert "background-position: center top" in page
    page = sa.build_html("B", "H", "", "", __file__, "light", (1080, 1350), "bottom")
    assert "background-position: center bottom" in page


def test_no_send_capability():
    """It renders a file. It does not mail it — every outbound email goes through the one
    guarded path on the Mac, and a second one hiding in a render script would be worse than
    the first because nobody would think to look here."""
    src = open(PATH).read()
    for banned in ("smtplib", "GMAIL_SEND", "send_email", "outbox"):
        assert banned not in src, f"{banned} appears in spec_ad.py"
