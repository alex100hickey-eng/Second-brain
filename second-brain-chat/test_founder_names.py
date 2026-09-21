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
