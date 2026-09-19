"""Tests for adlib_read.py --discover. No network: the pure parts only.

Discovery replaces ~20 minutes of hand browser work per sourcing run, so the two pieces that
make it cheap have to stay correct: the advertiser->page_id map that is only in the page's
embedded JSON, and the banding that decides whether a brand is worth emailing at all.
"""
import importlib.util
import os

SPEC = importlib.util.spec_from_file_location(
    "adlib_read", os.path.expanduser("~/second-brain/scripts/adlib_read.py"))
al = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(al)


def test_band_matches_the_offer_we_actually_sell():
    """5-50 is the sweet spot: enough ads to have a creative problem, too few for an in-house
    team. 100+ is an in-house team and the worst odds for a 19-year-old with no portfolio."""
    assert al.band_of(4) == "too_few"
    assert al.band_of(5) == "in" and al.band_of(50) == "in"
    assert al.band_of(51) == "out" and al.band_of(100) == "out"
    assert al.band_of(101) == "big" and al.band_of(3400) == "big"
    assert al.band_of(None) == "unknown"


def test_page_ids_come_out_of_the_embedded_json():
    """Advertiser names are React components, not anchors — a[href*=view_all_page_id] finds
    nothing on a results page. The map is in the JSON."""
    html = ('junk {"page_id":"1717506048563787","other":1,"page_name":"ForeverWick Candle"} more '
            '{"page_id":"104507196273121","page_name":"A Cheerful Giver Candle Company"}')
    pairs = [(m.group(2), m.group(1)) for m in al.PAGE_PAIR_RE.finditer(html)]
    assert ("ForeverWick Candle", "1717506048563787") in pairs
    assert ("A Cheerful Giver Candle Company", "104507196273121") in pairs


def test_a_page_id_must_look_like_one():
    """A short number is an index or a count, not a page id."""
    assert list(al.PAGE_PAIR_RE.finditer('{"page_id":"42","page_name":"Nope"}')) == []


def test_the_phrase_list_is_seller_size_not_category():
    """The whole point: a category keyword returns Subway and Lindt. These describe how a small
    seller talks about itself."""
    assert "small batch" in al.SELLER_PHRASES and "hand poured" in al.SELLER_PHRASES
    for p in al.SELLER_PHRASES:
        assert " " in p, f"{p!r} is a single word — too broad to stay seller-size"


def test_discover_asks_for_an_exact_phrase_search():
    """keyword_unordered would match the words anywhere and lose the seller-size signal."""
    import inspect
    src = inspect.getsource(al.discover)
    assert "keyword_exact_phrase" in src
    assert "view_all_page_id" in src, "counts must come from the advertiser's own page header"
