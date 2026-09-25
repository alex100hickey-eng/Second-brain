"""find_published_addresses.py: founders' own addresses from brands' own pages, and nothing else.

The sending rule is "named person or no send". This tool feeds it. It reads a brand's own pages
and reports addresses that read as a PERSON on the brand's own domain. Two failure modes would
make it worse than useless:
  - reporting an inbox (info@, support@, privacy@) or an off-domain address as a founder, and
  - constructing an address that the page never printed ("chat with Sarah at brand.com").
It must also never write the prospect tracker. Every fetch is faked here from local fixtures; an
autouse guard makes any real HTTP request fail the test.
"""
import csv
import hashlib
import importlib.util
import os

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "published_addresses")

# This checkout's own script, never ~/second-brain's: a worktree test that imports main's copy
# passes against code that is not the code under review.
_spec = importlib.util.spec_from_file_location(
    "find_published_addresses_under_test", os.path.join(ROOT, "scripts", "find_published_addresses.py"))
fap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fap)


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeWeb:
    """path -> list of (status, body, ctype); consumed in order, the last one repeats. Anything
    unrouted is a 404. Records every request (url, clock time)."""

    def __init__(self, base, routes, clock, redirect_home_to=None):
        self.base = base
        self.routes = {k: list(v) for k, v in routes.items()}
        self.clock = clock
        self.calls = []
        self.redirect_home_to = redirect_home_to

    def __call__(self, url):
        self.calls.append((url, self.clock.now()))
        path = url[len(self.base):] if url.startswith(self.base) else url
        seq = self.routes.get(path)
        if not seq:
            return fap.Page(404, url, "", "text/html")
        status, body, ctype = seq.pop(0) if len(seq) > 1 else seq[0]
        final = self.redirect_home_to if (path == "/" and self.redirect_home_to) else url
        return fap.Page(status, final, body, ctype, None)


@pytest.fixture(autouse=True)
def no_network_and_fake_time(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(requests.Session, "request", refuse)
    clock = FakeClock()
    monkeypatch.setattr(fap, "_now", clock.now)
    monkeypatch.setattr(fap, "_sleep", clock.sleep)
    return clock


def _html(name):
    return (200, _fixture(name), "text/html; charset=utf-8")


LOUDCUP_ROUTES = {
    "/": [_html("loudcup_home.html")],
    "/pages.json?limit=250": [(200, _fixture("loudcup_pages.json"), "application/json")],
    "/pages/contact": [_html("loudcup_contact.html")],
    "/pages/meet-the-team": [_html("loudcup_team.html")],
}


def _scan(monkeypatch, clock, base, routes, row, **kw):
    web = FakeWeb(base, routes, clock, **kw)
    monkeypatch.setattr(fap, "http_get", web)
    return fap.scan_domain(row), web


def _by_addr(res):
    return {r["address"]: r for r in res["results"]}


# --------------------------------------------------------------------------- extraction

def test_every_published_form_is_read_and_nothing_is_constructed():
    _text, hits, _off = fap.extract(_fixture("loudcup_contact.html"),
                                    "https://theloudcup.com/pages/contact", {"theloudcup.com"})
    found = {h.address for h in hits}
    assert {"jane@theloudcup.com",          # [at] ... [dot]
            "omar@theloudcup.com",          # (at)
            "priya@theloudcup.com",         # at ... dot, both spelled out
            "mkeller@theloudcup.com",       # plain text
            "nina@theloudcup.com",          # mailto: with %40
            "mark@mail.theloudcup.com",     # a subdomain is still the brand
            "justloudit@theloudcup.com"} <= found, found
    # "chat with Sarah at theloudcup.com" is a sentence about a website, not an address.
    assert "sarah@theloudcup.com" not in found


def test_cloudflare_entities_json_ld_and_off_domain():
    _text, hits, off = fap.extract(_fixture("loudcup_home.html"), "https://theloudcup.com/",
                                   {"theloudcup.com"})
    found = {h.address for h in hits}
    assert "zoe@theloudcup.com" in found        # Cloudflare data-cfemail decoded
    assert "leo@theloudcup.com" in found        # &#64; entity
    assert "kate@theloudcup.com" in found       # JSON-LD Organization email
    assert "hello@theloudcup.com" in found      # extracted here, dropped later as generic
    assert "jane@gmail.com" not in found
    assert "help@theloudcup.freshdesk.com" not in found   # a lookalike, not a subdomain
    assert off >= 2


def test_script_json_is_not_page_text():
    # builtbyswift.com's privacy page carries dealers@ only inside a theme <script> config.
    _text, hits, _off = fap.extract(_fixture("swift_privacy.html"),
                                    "https://builtbyswift.com/policies/privacy-policy",
                                    {"builtbyswift.com"})
    found = {h.address for h in hits}
    assert found == {"martina@builtbyswift.com"}
    assert "email us at martina@builtbyswift.com" in hits[0].snippet


def test_on_domain_is_exact_or_a_real_subdomain():
    assert fap.on_domain("curiebod.com", {"curiebod.com"})
    assert fap.on_domain("mail.curiebod.com", {"curiebod.com"})
    assert not fap.on_domain("curiecosmetics.com", {"curiebod.com"})
    assert not fap.on_domain("littleseedfarm.freshdesk.com", {"littleseedfarm.com"})
    assert not fap.on_domain("notcuriebod.com", {"curiebod.com"})


def test_a_glued_sentence_is_not_a_tld():
    assert fap._clean_address("sarah", "curiebod.com.Our", {"curiebod.com"}) == "sarah@curiebod.com"
    assert fap._clean_address("Sarah", "brand.com.au", {"brand.com.au"}) == "sarah@brand.com.au"


def test_json_escapes_do_not_leak_into_the_local_part():
    raw = r'<div>Reach us \u003einfo@theloudcup.com or zoe\u0040theloudcup.com</div>'
    _text, hits, _off = fap.extract(raw, "https://theloudcup.com/", {"theloudcup.com"})
    found = {h.address for h in hits}
    assert "info@theloudcup.com" in found and "zoe@theloudcup.com" in found
    assert not any(a.startswith("u003") for a in found)
    assert fap._clean_address("u003einfo", "theloudcup.com", {"theloudcup.com"}) == "info@theloudcup.com"


def test_generic_inboxes_are_never_people():
    for local in ("info", "hello", "support", "press", "sales", "orders", "wholesale", "privacy",
                  "customer.service", "customerservice", "hello-us", "sup", "dpo", "careers",
                  "help", "contact", "team", "jobs", "wholesale.orders", "support2", "yogisupport",
                  "europe", "uk"):
        assert fap.is_generic(local), local
    for local in ("sarah", "martina", "jane.doe", "mkeller", "wrenna"):
        assert not fap.is_generic(local), local


# --------------------------------------------------------------------------- scoring, end to end

def test_loudcup_tiers(monkeypatch, no_network_and_fake_time):
    res, _web = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com",
                      LOUDCUP_ROUTES, {"brand": "Loudcup", "domain": "theloudcup.com",
                                       "contact_name": ""})
    got = _by_addr(res)
    # An unknown first name becomes a named founder because the site says who founded it.
    w = got["wrenna@theloudcup.com"]
    assert w["tier"].startswith("named") and "founder" in w["tier"]
    assert w["person_name_if_shown"] == "Wrenna Voss"
    assert w["page_url"] == "https://theloudcup.com/pages/data-requests"
    # A name printed beside the address, spelling its local part.
    k = got["mkeller@theloudcup.com"]
    assert k["tier"].startswith("named") and k["person_name_if_shown"] == "Martina Keller"
    # A mailto link whose text names the person ("Write to Nina") counts as a name beside it.
    n = got["nina@theloudcup.com"]
    assert n["tier"].startswith("named") and n["person_name_if_shown"] == "Nina"
    assert n["evidence_snippet"].startswith("mailto link: nina@theloudcup.com")
    # Known first names with nobody confirming them.
    for a in ("jane@theloudcup.com", "omar@theloudcup.com", "priya@theloudcup.com",
              "zoe@theloudcup.com", "leo@theloudcup.com",
              "kate@theloudcup.com", "mark@mail.theloudcup.com"):
        assert got[a]["tier"] == "first_name", (a, got[a])
        assert got[a]["person_name_if_shown"] == ""
    # Inboxes and brand-voice addresses are not results at all.
    assert "hello@theloudcup.com" not in got
    assert "justloudit@theloudcup.com" not in got
    assert res["excluded"]["generic"] >= 1 and res["excluded"]["not a person"] >= 1
    # Named rows sort first.
    tiers = [r["tier"] for r in res["results"]]
    assert tiers == sorted(tiers, key=lambda t: not t.startswith("named"))
    for r in res["results"]:
        assert list(r.keys()) == fap.COLUMNS


def test_the_curie_privacy_policy_yields_sarah(monkeypatch, no_network_and_fake_time):
    routes = {"/": [(200, "<html><body><p>Clean deodorant.</p></body></html>", "text/html")],
              "/policies/privacy-policy": [_html("curie_privacy.html")]}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://curiebod.com", routes,
                   {"brand": "Curie", "domain": "curiebod.com"})
    got = _by_addr(res)
    assert set(got) == {"sarah@curiebod.com"}
    s = got["sarah@curiebod.com"]
    assert s["tier"] == "first_name"
    assert s["page_url"] == "https://curiebod.com/policies/privacy-policy"
    assert "by contacting us at sarah@curiebod.com" in s["evidence_snippet"]
    assert res["excluded"]["off-domain"] >= 1          # sarah@curiecosmetics.com
    assert res["excluded"]["generic"] >= 2             # privacy@, support@


def test_tracker_contact_name_makes_it_named(monkeypatch, no_network_and_fake_time):
    routes = {"/": [(200, "<p>Say hi: wrenna@theloudcup.com</p><p>Wrenna and the crew</p>",
                     "text/html")]}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                   {"brand": "Loudcup", "domain": "theloudcup.com", "contact_name": "Wrenna Voss"})
    r = _by_addr(res)["wrenna@theloudcup.com"]
    assert r["tier"] == "named (tracker contact_name)"
    assert r["person_name_if_shown"] == "Wrenna Voss"


def test_a_heading_beside_the_address_is_not_a_person(monkeypatch, no_network_and_fake_time):
    """The first real run (2026-09-25) called europe@manduka.com 'named: Europe Hours', from
    'Europe Hours of Operation ... Wholesale inquiries: europe@manduka.com'."""
    page = ("<p>Europe Hours of Operation: Monday - Friday, 9am - 5pm CET</p>"
            "<p>Wholesale inquiries: europe@theloudcup.com</p>"
            "<p>Tours: Rosa Hours runs them, rosa@theloudcup.com</p>")
    routes = {"/": [(200, page, "text/html")]}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                   {"brand": "Loudcup", "domain": "theloudcup.com"})
    got = _by_addr(res)
    assert "europe@theloudcup.com" not in got
    # A real first name beside its own address still counts.
    assert got["rosa@theloudcup.com"]["person_name_if_shown"] == "Rosa Hours"


def test_unknown_local_part_without_evidence_is_not_a_result(monkeypatch, no_network_and_fake_time):
    routes = {"/": [(200, "<p>Say hi: wrenna@theloudcup.com</p>", "text/html")]}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                   {"brand": "Loudcup", "domain": "theloudcup.com"})
    assert res["results"] == []
    assert res["excluded"]["not a person"] == 1


def test_the_brands_own_name_is_not_a_person(monkeypatch, no_network_and_fake_time):
    page = "<p>Treat orders: gracie@graciesdoggiedelights.com. Gracie says woof.</p>"
    routes = {"/": [(200, page, "text/html")]}
    row = {"brand": "Gracie's Doggie Delights", "domain": "graciesdoggiedelights.com"}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://graciesdoggiedelights.com",
                   routes, row)
    assert res["results"] == []
    # ...unless the tracker already names her.
    routes = {"/": [(200, page, "text/html")]}
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://graciesdoggiedelights.com",
                   routes, dict(row, contact_name="Gracie Smith"))
    assert _by_addr(res)["gracie@graciesdoggiedelights.com"]["tier"].startswith("named")


def test_a_homepage_redirect_widens_the_domain(monkeypatch, no_network_and_fake_time):
    web = FakeWeb("https://oldbrand.com", {}, no_network_and_fake_time,
                  redirect_home_to="https://www.newbrand.co/")
    web.routes["/"] = [(200, "<p>Founder mail: sarah@newbrand.co</p>", "text/html")]

    def route(url):
        if url.startswith("https://www.newbrand.co"):
            web.calls.append((url, no_network_and_fake_time.now()))
            return fap.Page(404, url, "", "text/html")
        return web(url)
    monkeypatch.setattr(fap, "http_get", route)
    res = fap.scan_domain({"brand": "Newbrand", "domain": "oldbrand.com"})
    assert "sarah@newbrand.co" in _by_addr(res)
    assert all(u.startswith("https://www.newbrand.co") for u, _ in web.calls[1:])


# --------------------------------------------------------------------------- politeness

def test_one_request_per_second_per_domain(monkeypatch, no_network_and_fake_time):
    _res, web = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com",
                      LOUDCUP_ROUTES, {"brand": "Loudcup", "domain": "theloudcup.com"})
    times = [t for _u, t in web.calls]
    assert len(times) >= len(fap.PATHS)
    assert all(b - a >= fap.MIN_GAP - 1e-9 for a, b in zip(times, times[1:]))


def test_403_is_retried_once_then_skipped(monkeypatch, no_network_and_fake_time):
    routes = dict(LOUDCUP_ROUTES)
    routes["/policies/privacy-policy"] = [(403, "", "text/html")]
    res, web = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                     {"brand": "Loudcup", "domain": "theloudcup.com"})
    hits = [u for u, _ in web.calls if u.endswith("/policies/privacy-policy")]
    assert len(hits) == 2
    assert "wrenna@theloudcup.com" in _by_addr(res)       # the rest of the site still read


def test_429_then_200_is_read(monkeypatch, no_network_and_fake_time):
    routes = dict(LOUDCUP_ROUTES)
    routes["/pages/contact"] = [(429, "", "text/html"), _html("loudcup_contact.html")]
    res, _ = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                   {"brand": "Loudcup", "domain": "theloudcup.com"})
    assert "mkeller@theloudcup.com" in _by_addr(res)


def test_a_domain_that_keeps_refusing_is_left_alone(monkeypatch, no_network_and_fake_time):
    routes = {"/": [_html("loudcup_home.html")]}
    for p in fap.PATHS[1:]:
        routes[p] = [(403, "", "text/html")]
    res, web = _scan(monkeypatch, no_network_and_fake_time, "https://theloudcup.com", routes,
                     {"brand": "Loudcup", "domain": "theloudcup.com"})
    assert len(web.calls) == 1 + 2 * fap.BLOCK_STREAK
    assert res["status"] == "blocked"


def test_an_unreachable_domain_is_reported_not_raised(monkeypatch, no_network_and_fake_time):
    monkeypatch.setattr(fap, "http_get", lambda url: fap.Page(0, url, "ConnectionError", ""))
    res = fap.scan_domain({"brand": "Gone", "domain": "gone-brand.com"})
    assert res["status"] == "unreachable" and res["results"] == []


# --------------------------------------------------------------------------- selection

def _row(**kw):
    r = {"brand": "Loudcup", "domain": "theloudcup.com", "status": "qualified", "email": "",
         "email_status": "", "sent_date": "", "contact_name": ""}
    r.update(kw)
    return r


def test_which_rows_are_scanned():
    assert fap.eligible(_row())
    assert fap.eligible(_row(email="info@theloudcup.com"))
    assert fap.eligible(_row(email="hello@theloudcup.com", email_status="deliverable"))
    assert not fap.eligible(_row(email="hello@theloudcup.com", email_status="sent"))
    assert not fap.eligible(_row(email="support@theloudcup.com", sent_date="2026-09-22"))
    assert not fap.eligible(_row(email="wrenna@theloudcup.com"))      # already has a person
    assert not fap.eligible(_row(status="candidate"))
    assert not fap.eligible(_row(domain="twitch.tv/somecreator", email="contact@creator.com"))
    assert fap.eligible(_row(status="Qualified", domain="https://www.theloudcup.com/"))


def test_selection_orders_unsent_first_dedupes_and_limits():
    rows = [_row(brand="A", domain="a.com", sent_date="2026-09-01"),
            _row(brand="B", domain="b.com"),
            _row(brand="B again", domain="www.b.com"),
            _row(brand="C", domain="c.com")]
    picked = fap.select_rows(rows)
    assert [r["brand"] for r in picked] == ["B", "C", "A"]
    assert [r["brand"] for r in fap.select_rows(rows, limit=1)] == ["B"]
    # --only scans the named domain even when it is not eligible, and even if it is not tracked.
    assert fap.select_rows(rows, only="a.com")[0]["brand"] == "A"
    assert fap.select_rows(rows, only="curiebod.com") == [{"brand": "curiebod.com",
                                                            "domain": "curiebod.com"}]


# --------------------------------------------------------------------------- tracker and side file

def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tracker(tmp_path):
    p = tmp_path / "prospect-tracker.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "domain", "status", "email", "email_status",
                                          "sent_date", "contact_name"])
        w.writeheader()
        w.writerow(_row())
        w.writerow(_row(brand="Sent", domain="sent.com", email="info@sent.com", sent_date="2026-09-01"))
    return p


def test_main_writes_the_side_file_and_never_the_tracker(monkeypatch, tmp_path, no_network_and_fake_time):
    tracker = _tracker(tmp_path)
    before = _sha(tracker)
    monkeypatch.setattr(fap, "TRACKER", str(tracker))
    web = FakeWeb("https://theloudcup.com", LOUDCUP_ROUTES, no_network_and_fake_time)
    monkeypatch.setattr(fap, "http_get", web)
    out = tmp_path / "Published Addresses — test.csv"

    assert fap.main(["--dry-run", "--out", str(out), "--workers", "1"]) == 0
    assert not out.exists()
    assert all("sent.com" not in u for u, _ in web.calls)     # a sent row is not scanned

    web2 = FakeWeb("https://theloudcup.com", LOUDCUP_ROUTES, no_network_and_fake_time)
    monkeypatch.setattr(fap, "http_get", web2)
    assert fap.main(["--out", str(out), "--workers", "1"]) == 0
    with open(out, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        assert rd.fieldnames == fap.COLUMNS
        rows = list(rd)
    assert rows[0]["tier"].startswith("named")
    assert {r["address"] for r in rows} >= {"wrenna@theloudcup.com", "mkeller@theloudcup.com"}
    assert _sha(tracker) == before

    # A re-run merges instead of wiping, and keeps the first found_at.
    first_seen = {r["address"]: r["found_at"] for r in rows}
    fap.write_results(str(out), [dict(rows[0], found_at="2099-01-01T00:00:00")])
    with open(out, newline="", encoding="utf-8") as f:
        again = list(csv.DictReader(f))
    assert len(again) == len(rows)
    assert {r["address"]: r["found_at"] for r in again} == first_seen


def test_a_rescanned_brand_replaces_its_old_rows(tmp_path):
    out = str(tmp_path / "side.csv")
    base = {"brand": "Loudcup", "domain": "theloudcup.com", "page_url": "u", "evidence_snippet": "e",
            "person_name_if_shown": "", "tier": "first_name"}
    fap.write_results(out, [dict(base, address="europe@theloudcup.com", found_at="t0"),
                            dict(base, address="zoe@theloudcup.com", found_at="t0"),
                            dict(base, brand="Other", domain="other.com",
                                 address="kate@other.com", found_at="t0")])
    # theloudcup.com re-scanned with the fixed code: europe@ is gone, zoe@ keeps its first
    # found_at, and a brand that was not re-scanned is untouched.
    fap.write_results(out, [dict(base, address="zoe@theloudcup.com", found_at="t1")],
                      scanned_domains=["theloudcup.com"])
    with open(out, newline="", encoding="utf-8") as f:
        rows = {r["address"]: r for r in csv.DictReader(f)}
    assert set(rows) == {"zoe@theloudcup.com", "kate@other.com"}
    assert rows["zoe@theloudcup.com"]["found_at"] == "t0"


def test_the_tracker_path_is_refused_as_output(monkeypatch, tmp_path):
    tracker = _tracker(tmp_path)
    monkeypatch.setattr(fap, "TRACKER", str(tracker))
    before = _sha(tracker)
    with pytest.raises(SystemExit):
        fap.write_results(str(tracker), [])
    with pytest.raises(SystemExit):
        fap.write_results(str(tmp_path / "elsewhere" / "prospect-tracker.csv"), [])
    assert _sha(tracker) == before


def test_an_evicted_tracker_asks_icloud_then_reads_the_git_mirror(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(fap, "_run", lambda cmd: ran.append(cmd))
    monkeypatch.setattr(fap, "mirror_text",
                        lambda: "brand,domain,status,email\nLoudcup,theloudcup.com,qualified,\n")
    missing = str(tmp_path / "evicted.csv")
    rows, source = fap.load_tracker(missing)
    assert ran and ran[0][:2] == ["brctl", "download"]
    assert source == "git mirror" and rows[0]["brand"] == "Loudcup"

    empty = tmp_path / "empty.csv"
    empty.write_text("")
    rows, source = fap.load_tracker(str(empty))
    assert source == "git mirror"


def test_the_module_has_no_send_or_tracker_write_path():
    with open(os.path.join(ROOT, "scripts", "find_published_addresses.py"), encoding="utf-8") as f:
        src = f.read()
    for banned in ("smtplib", "send_message", "sendmail", "gmail", "splitframe_send"):
        assert banned not in src, banned
    # The tracker is only ever opened for reading.
    assert 'open(p, "r"' in src
    assert "open(TRACKER" not in src
