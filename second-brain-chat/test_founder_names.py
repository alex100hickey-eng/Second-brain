"""The founder-name scraper must never invent a greeting.

Its own doctrine: "A wrong name is worse than no name: 'Hi Sarah' at a company with no Sarah is an
instant delete and reads exactly like the blast this pitch depends on not being."

On 2026-09-19 it broke that rule. Hedley & Bennett's /about carries the Shopify section catalogue as
JSON inside an HTML attribute, so strip_html turned 607,672 characters of product data into "page
text". Buried in it was a video caption for a COLLABORATOR's brand — "Fatima, owner of KOMAL
Cookware" — which matched the `NAME, owner` pattern at full weight and outscored the real founder
page. The scraper's verdict was "Fatima", for a company founded by Ellen Bennett.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/second-brain/scripts"))
import find_founder_names as ff  # noqa: E402


REAL_PAGE = "<h1>ELLEN MARIE BENNETT</h1><p>Hedley &amp; Bennett was founded by Ellen Bennett.</p>"


def test_json_inside_an_attribute_is_not_page_text():
    html = ('<div data-section=\'{"_type":"shopifyImage","altText":"Fatima, owner of KOMAL '
            'Cookware, takes us inside the process."}\'>Our Story</div>')
    assert "Fatima" not in ff.strip_html(html)


def test_a_script_block_is_not_page_text():
    html = '<script>var d = {"altText":"Fatima, owner of KOMAL"};</script><p>Our Story</p>'
    assert "Fatima" not in ff.strip_html(html)


def test_real_prose_survives_the_scrub():
    # The scrub must not be so aggressive that it eats the sentence we are looking for.
    out = ff.strip_html(REAL_PAGE)
    assert "Ellen Bennett" in out
    assert "founded by" in out


def test_the_collaborator_caption_cannot_become_a_candidate():
    """End to end on the pattern layer: the exact string that produced the wrong verdict."""
    text = ff.strip_html(
        '<div data-x=\'{"altText":"Fatima, owner of KOMAL Cookware"}\'></div>'
        '<p>Hedley &amp; Bennett was founded by Ellen Bennett.</p>')
    names = set()
    for pat, _w in ff.PATTERNS:
        for m in pat.finditer(text):
            names.add(m.group(1).strip())
    assert not any("Fatima" in n for n in names), names
    assert any("Ellen" in n for n in names), names


def test_a_data_dump_page_is_skipped_outright():
    # Belt and braces: even if some JSON survives the scrub, a 600k-char "About page" is not prose
    # and must not get a vote.
    assert ff.MAX_PAGE_CHARS < 100000
    assert len(ff.strip_html("<p>x</p>" * 200000)) > ff.MAX_PAGE_CHARS


def test_plausible_still_rejects_the_brand_name_itself():
    banned = ff.brand_words("Hedley & Bennett", "hedleyandbennett.com")
    first = ff.load_first_names()
    assert not ff.plausible("Hedley Bennett", banned, first, 5)


# ---------------------------------------------------------------------------
# Throttling is an unknown, not a no.
#
# 2026-09-21: bulk runs reported 0/45, 0/35 and 0/40 — read as "these brands publish no founder",
# and written into the tracker as `no clear founder name`. A probe of 30 of those domains returned
# HTTP 429 on 29 of them: a shared CDN edge was rate-limiting our IP because the crawler tried up
# to thirteen URLs per brand, 0.3s apart, and FAILED fetches did not count against the page budget
# — so a site whose first paths 404'd got the whole list hammered.
#
# The negatives were artifacts. Recording them as fact is the expensive part: a human reading the
# row believes someone looked, when nobody did.
# ---------------------------------------------------------------------------

def test_every_attempt_counts_against_the_budget():
    # The bug: only successes were counted, so a 404-heavy site got every path tried.
    assert ff.MAX_FETCHES <= len(ff.PATHS), "budget must be able to bind before the path list ends"


def test_the_crawl_is_slow_enough_to_be_served():
    import inspect
    assert "time.sleep(0.6)" in inspect.getsource(ff.names_for)


def test_a_throttled_brand_reports_unknown_not_no(monkeypatch):
    import urllib.error

    def always_429(url, timeout=9):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(ff, "fetch", always_429)
    name, title, ev, pages = ff.names_for("Goldilocks Goods", "goldilocksgoods.com",
                                          ff.load_first_names())
    assert name is None
    assert pages == 0
    assert title == "throttled", "a rate-limited read must be distinguishable from a real miss"


def test_a_genuine_miss_is_still_reported_as_a_miss(monkeypatch):
    # The other half of the contract: a site we DID read, with no founder on it, is a real no.
    monkeypatch.setattr(ff, "fetch", lambda url, timeout=9: "<p>We sell candles.</p>")
    name, title, ev, pages = ff.names_for("Nose Dive Scents", "nosedivescents.com",
                                          ff.load_first_names())
    assert name is None
    assert title != "throttled"
    assert pages > 0


def test_a_throttled_run_stops_hammering_the_site(monkeypatch):
    import urllib.error
    calls = []

    def count_429(url, timeout=9):
        calls.append(url)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(ff, "fetch", count_429)
    ff.names_for("Goldilocks Goods", "goldilocksgoods.com", ff.load_first_names())
    # The homepage read plus at most one more: once 429 comes back, backing off is the only
    # behaviour that gets us served again.
    assert len(calls) <= 2, f"kept hammering after a 429: {len(calls)} requests"


def test_the_caption_pattern_finds_a_name_after_its_title():
    # "Amy Hall Our Founder" — name first, possessive title after, which is how a photo caption
    # reads. goldilocksgoods.com names its founder that way and the scraper returned nothing.
    text = ff.strip_html("<p>Amy Hall Our Founder is an art historian and ocean lover.</p>")
    found = {m.group(1).strip() for pat, _w in ff.PATTERNS for m in pat.finditer(text)}
    assert any("Amy Hall" in n for n in found), found
