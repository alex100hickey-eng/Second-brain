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


# ---------------------------------------------------------------------------
# Reply detection at scale. A prospect replying is the single most valuable
# event this business can see, and the watcher was one Gmail query listing
# every un-replied brand. Gmail stops honouring a search near 2,000 chars, the
# fetch was wrapped in `except: return []`, and "no replies" is what you see
# whether nobody answered or the watcher died.
# ---------------------------------------------------------------------------

def _acp():
    import importlib.util, os, sys
    sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
    import ad_creative_pipeline as m
    return m


def test_the_query_stays_under_gmails_limit_at_real_volume():
    """At 33 sends the query was 649 chars. A three-touch sequence keeps a brand on the watch
    list about eight days, so 20/day settles near 160 domains — roughly 3,000 characters."""
    acp = _acp()
    domains = [f"brand{i:03d}example.com" for i in range(300)]
    chunks = acp._domain_chunks(domains)
    assert chunks, "must produce batches"
    for c in chunks:
        q = "from:(" + " OR ".join(c) + ") newer_than:2d"
        assert len(q) < 2000, f"chunk query is {len(q)} chars"


def test_chunking_never_drops_a_brand():
    """A dropped domain is a prospect whose reply is invisible forever."""
    acp = _acp()
    domains = [f"b{i}.com" for i in range(137)]
    flat = [d for c in acp._domain_chunks(domains) for d in c]
    assert sorted(flat) == sorted(domains)
    assert len(flat) == len(set(flat)), "no duplicates either"


def test_small_and_empty_lists_are_handled():
    acp = _acp()
    assert acp._domain_chunks([]) == []
    assert acp._domain_chunks(["a.com", "b.com"]) == [["a.com", "b.com"]]


def test_one_failing_batch_does_not_lose_the_others():
    """Losing one batch is bad; losing every brand because the first batch failed is worse."""
    acp = _acp()

    calls = {"n": 0}

    def fetch(q):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gmail said no")
        return [{"id": "m1", "sender": "founder@brand001example.com", "subject": "re: your email"}]

    def fake_domains():
        return {f"brand{i:03d}example.com": f"Brand{i}" for i in range(200)}

    orig = acp.sent_domains
    acp.sent_domains = fake_domains
    try:
        hits = acp.detect_prospect_replies(fetch)
    finally:
        acp.sent_domains = orig
    assert calls["n"] > 1, "must keep going after a failed batch"
    assert any(h["id"] == "m1" for h in hits), "the reply in a later batch must still be found"
