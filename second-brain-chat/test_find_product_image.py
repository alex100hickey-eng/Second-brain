"""Tests for scripts/find_product_image.py. No network — the pure functions only.

This exists because the script's first run returned a GIFT CARD as Gracie's Doggie Delights'
product photo, and the whole point of the file is that the photo must be the thing the email
promises. A finder that confidently returns the wrong image is worse than no finder.
"""
import importlib.util
import os

SPEC = importlib.util.spec_from_file_location(
    "find_product_image", os.path.expanduser("~/second-brain/scripts/find_product_image.py"))
fpi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fpi)


def test_non_products_are_excluded_by_slug():
    """A gift card is listed under /products/ and is not a thing you photograph for an ad."""
    for slug in ("/products/gift-card", "/products/e-gift-card", "/products/giftcard",
                 "/products/shipping-protection", "/products/route-insurance",
                 "/products/extended-warranty", "/products/donation"):
        assert fpi.NOT_A_PRODUCT_SLUG.search(slug), slug
    for slug in ("/products/beef-liver-delights", "/products/3-x-6-pillar-candle",
                 "/products/body-serum", "/products/deer-antler-velvet-capsules"):
        assert not fpi.NOT_A_PRODUCT_SLUG.search(slug), slug


def test_share_cards_and_logos_are_not_product_photos():
    """Busy Bees' opengraph card was stored as 'the pillar candle photo on your site'."""
    for url in ("https://x.com/cdn/shop/files/opengraph.jpg", "https://x.com/og-image.png",
                "https://x.com/files/logo.png", "https://x.com/social-share.jpg",
                "https://x.com/files/visa.png", "https://x.com/hero-bg.jpg"):
        assert fpi.NOT_A_PRODUCT.search(url), url
    assert not fpi.NOT_A_PRODUCT.search(
        "https://x.com/cdn/shop/files/BusyBeesCandles-0825.jpg")


def test_a_named_product_never_falls_back_to_a_different_one(monkeypatch):
    """Searching for the BODY serum returned the BABY serum, because 'best of the rest' ran
    when nothing matched. The email names a product out loud, so a near miss is a wrong claim."""
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<a href="/products/baby-serum">x</a><a href="/products/face-cream">y</a>'))
    assert fpi.product_links("https://x.com", "body serum") == []
    # and with a real match, only the matches come back
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<a href="/products/baby-serum">x</a><a href="/products/body-serum">y</a>'))
    assert fpi.product_links("https://x.com", "body serum") == ["/products/body-serum"]


def test_no_match_filter_returns_the_catalogue(monkeypatch):
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<a href="/products/a">x</a><a href="/products/b">y</a>'))
    assert sorted(fpi.product_links("https://x.com")) == ["/products/a", "/products/b"]


def test_products_on_someone_elses_domain_are_skipped(monkeypatch):
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<a href="https://otherstore.com/products/theirs">x</a>'
        '<a href="/products/ours">y</a>'))
    assert fpi.product_links("https://x.com") == ["/products/ours"]


def test_protocol_relative_and_absolute_urls_normalise():
    assert fpi.normalise("//x.com/a.jpg", "https://x.com") == "https://x.com/a.jpg"
    assert fpi.normalise("/cdn/a.jpg", "https://x.com") == "https://x.com/cdn/a.jpg"
    assert fpi.normalise("https://x.com/a.jpg", "https://x.com") == "https://x.com/a.jpg"


def test_a_page_whose_og_image_is_the_site_share_card_yields_nothing(monkeypatch):
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<meta property="og:image" content="https://x.com/files/opengraph.jpg">'
        '<meta property="og:title" content="Pillar Candle">'))
    img, title = fpi.image_for_product("https://x.com/products/p", "https://x.com")
    assert img is None


def test_a_real_product_page_yields_image_and_title(monkeypatch):
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<meta property="og:image" content="//x.com/cdn/shop/files/BusyBees-0825.jpg?v=1">'
        '<meta property="og:title" content="3&quot; x 6&quot; Pillar Candle">'))
    img, title = fpi.image_for_product("https://x.com/products/p", "https://x.com")
    assert img == "https://x.com/cdn/shop/files/BusyBees-0825.jpg?v=1"
    assert title == '3" x 6" Pillar Candle'


def test_non_image_og_content_is_rejected(monkeypatch):
    monkeypatch.setattr(fpi, "fetch", lambda url, timeout=10: (
        '<meta property="og:image" content="https://x.com/video.mp4">'))
    img, _t = fpi.image_for_product("https://x.com/products/p", "https://x.com")
    assert img is None


# --- the founder-name finder's own false positive, kept here beside its sibling tool ---

def _ffn():
    import importlib.util, os
    s = importlib.util.spec_from_file_location(
        "find_founder_names", os.path.expanduser("~/second-brain/scripts/find_founder_names.py"))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_a_name_is_never_a_prefix_of_a_longer_token():
    """"Site Created by GoldBear.Media" produced the founder name "Gold" for Goose Ridge Soaps.
    Greeting someone "Gold" is exactly the mail-merge tell the greeting rule exists to avoid."""
    f = _ffn()
    text = "Copyright 2026 Goose Ridge Soaps, LLC. Site Created by GoldBear.Media"
    names = f.load_first_names()
    accepted = [m.group(1) for pat, w in f.PATTERNS for m in pat.finditer(text)
                if f.plausible(m.group(1), {"goose", "ridge", "soaps"}, names, w)
                and not f.is_credit_line(text, m.start())]
    assert accepted == [], accepted


def test_vendor_credits_are_not_founder_stories():
    """'created by', 'designed by', 'built by' introduce a web designer as often as a founder."""
    f = _ffn()
    for credit in ("Site Created by Jane Smith", "Website designed by Mark Jones",
                   "Store built by Sarah Lee", "Photography by Emily Ross"):
        assert f.is_credit_line(credit, credit.rindex("by") + 3), credit
    assert not f.is_credit_line("Founded by Jane Smith", len("Founded by "))


def test_a_real_founder_line_still_matches():
    """The guard must not cost recall on the cases the tool exists for."""
    f = _ffn()
    text = "Co-Founders Eileen & James Ray"
    names = f.load_first_names()
    accepted = [m.group(1) for pat, w in f.PATTERNS for m in pat.finditer(text)
                if f.plausible(m.group(1), {"little", "seed", "farm"}, names, w)
                and not f.is_credit_line(text, m.start())]
    assert "Eileen" in accepted
