#!/usr/bin/env python3
"""Fill in the `contact_name` column of the prospect tracker from brands' own public sites.

Why this exists (2026-09-18): all 23 first touches ever sent went to a NAMED founder
(klee@, jennifer@, melanie@ …). Those rows are used up. Every in-band brand left in the
tracker carries only a front-desk address — hello@, info@, contact@ — and
`target_address` deliberately refuses to invent a greeting for a shared inbox, so the
next 150 emails were all going to open cold with no name on them.

That is the term in the funnel that degrades the reply rate, and it is fixable for free:
small DTC brands publish the founder's name on their own About / Our Story page. This
reads those pages and writes the name into `contact_name`, which is all the drafter
needs — `splitframe_queue.py status` already prints the tracker's contact name for a
front-desk row, so a named front desk gets greeted by a human name instead of nothing.

PRECISION OVER RECALL, deliberately. A wrong name is worse than no name: "Hi Sarah" at a
company with no Sarah is an instant delete and reads exactly like the blast this pitch
depends on not being. So a name is only written when
  - it comes from an explicit founder/owner phrase ("founded by X", "X, Co-Founder"),
  - one candidate clearly outscores every other on the site, and
  - it survives the brand-name and common-word filters.
Everything else writes nothing and says why. A miss costs a greeting; a false hit costs
the prospect.

The evidence sentence is stored in `contact_name_source` so the claim is checkable later
by a human or by the drafting worker — the same rule as `--evidence` on the queue.

Usage:
    python3 scripts/find_founder_names.py                       # dry run, in-band unsent rows
    python3 scripts/find_founder_names.py --write
    python3 scripts/find_founder_names.py --brand "Cape Candle" --verbose
    python3 scripts/find_founder_names.py --limit 5 --write
"""
import argparse
import csv
import html as html_mod
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TRACKER = os.environ.get("VAULT_TRACKER") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain/"
    "Money/prospect-tracker.csv")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")

# About pages first here, the opposite order from find_prospect_emails.py: a footer carries
# the contact address, but the founder's name lives in the story.
PATHS = ["/pages/about", "/pages/about-us", "/pages/our-story", "/pages/story",
         "/about", "/about-us", "/our-story", "/pages/founder",
         "/pages/meet-the-founder", "/pages/who-we-are", "/pages/our-mission", ""]

# A human name: one to three capitalised tokens, allowing O'Brien and Smith-Jones.
NAME = r"([A-Z][a-z'’\-]{1,15}(?:\s+[A-Z][a-z'’\.\-]{1,20}){0,2})(?![A-Za-z])"
# The trailing (?![A-Za-z]) is load-bearing: without it the name could match a PREFIX of a
# longer CamelCase token. "Site Created by GoldBear.Media" — a web designer's footer credit —
# yielded the founder name "Gold" for Goose Ridge Soaps, and "Gold —" is exactly the
# mail-merge tell the whole greeting rule exists to avoid.

# Each pattern carries a weight: an explicit founder phrase is worth far more than "Meet X",
# which on a DTC site is as likely to introduce a dog or a product as a person.
PATTERNS = [
    # Case-insensitivity is scoped to the TITLE words only, with (?i:...). Making a whole
    # pattern re.I would also loosen NAME's [A-Z], and then every lowercase word in the
    # sentence becomes a candidate surname. That mistake cost an afternoon: "Co-Founders
    # Eileen" matched nothing at all because `co-?founders?` is lowercase in the source.
    (re.compile(r"\b(?i:founded|started|created|launched|co-?founded)\s+(?:in\s+\d{4}\s+)?"
                r"(?i:by)\s+" + NAME), 5),
    (re.compile(NAME + r"\s*,\s*(?i:(?:the\s+|our\s+)?(?:co-?)?(?:founder|owner|ceo))\b"), 5),
    (re.compile(r"\b(?i:(?:our|the)\s+(?:co-?)?(?:founder|owner))\s*,?\s+" + NAME), 4),
    (re.compile(NAME + r"\s+(?i:(?:is|was)\s+(?:the|our|a)\s+(?:co-?)?(?:founder|owner|ceo))\b"), 4),
    (re.compile(NAME + r"\s*[,\u2013\u2014-]\s*(?i:founder|co-?founder|owner|ceo)\b"), 4),
    (re.compile(r"\b(?i:founder|owner|ceo)\s*[:\u2013\u2014-]\s*" + NAME), 4),
    # "Amanda Chantal Bacon Founder & CEO", "-Sarah CEO & FOUNDER" — the signature block,
    # name first, title straight after with no comma. Safe because plausible() throws out
    # any candidate containing a stop word, so "A Note From Our Founder" yields nothing.
    (re.compile(NAME + r"\s+(?i:(?:co-?)?founder|ceo|owner)\b"), 4),
    # "Co-Founders Eileen & James Ray" — the title runs straight into the name with no
    # punctuation at all, which is how most About pages caption a founder photo.
    (re.compile(r"\b(?i:(?:co-?)?(?:founders?|owners?))\s+" + NAME), 4),
    # "Amy Hall Our Founder", "Randy McMillan, Our Founder" — the name first and the title after
    # with a possessive in between, which is how a photo caption reads. The plain NAME+title
    # pattern above misses these because of the intervening word. Found on goldilocksgoods.com
    # 2026-09-21, where the founder is named in plain text and the scraper returned nothing.
    (re.compile(NAME + r"\s*,?\s+(?i:(?:our|the)\s+(?:co-?)?(?:founder|owner|ceo))\b"), 4),
    (re.compile(r"\b(?i:i'?m)\s+" + NAME + r"[,.]?\s+(?i:(?:the\s+)?(?:founder|owner)|and i)"), 4),
    (re.compile(r"\b(?i:hi,?\s*i'?m)\s+" + NAME), 2),
    (re.compile(r"\b(?i:meet)\s+" + NAME), 1),
]

# Capitalised words that match the NAME shape but are never the founder. Kept deliberately
# broad — a word wrongly excluded costs one greeting; a word wrongly accepted costs a prospect.
STOP = {
    "the", "our", "we", "us", "this", "that", "your", "you", "my", "me", "it", "its",
    "shop", "home", "about", "story", "team", "family", "founder", "founders", "owner",
    "ceo", "company", "brand", "collection", "new", "all", "more", "learn", "read",
    "contact", "faq", "blog", "cart", "menu", "search", "account", "login", "join",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "today", "mother", "father", "nature",
    "god", "earth", "sea", "sun", "moon", "made", "usa", "america", "american", "united",
    "states", "california", "texas", "montana", "colorado", "oregon", "washington",
    "york", "jersey", "carolina", "dakota", "florida", "georgia", "hawaii", "maine",
    "free", "shipping", "order", "orders", "returns", "policy", "privacy", "terms",
    "service", "support", "help", "gift", "gifts", "sale", "best", "seller", "sellers",
    "customer", "customers", "review", "reviews", "quality", "small", "batch", "hand",
    "love", "life", "day", "days", "year", "years", "first", "second", "third",
    "everything", "everyone", "something", "nothing", "anyone", "because", "since",
    "when", "where", "what", "why", "how", "who", "after", "before", "while", "with",
    "from", "into", "over", "under", "every", "each", "both", "some", "many", "much",
    "here", "there", "then", "now", "soon", "just", "only", "also", "even", "still",
    "meet", "note", "letter", "word", "message", "unlock", "tip", "tips", "view", "see",
    "welcome", "hello", "thanks", "thank", "sincerely", "xo", "love", "follow", "shop",
}

MAX_PAGES = 6           # per brand: enough to reach the real About page, few enough to stay polite
# A real About page is a few thousand characters of prose. Anything vastly larger is a data
# dump, and its noise drowns the page that actually names the founder.
MAX_PAGE_CHARS = 60000
# Total HTTP requests per brand, successes and failures alike. Thirteen rapid requests
# reads as a scan; this keeps the crawl polite enough to be served.
MAX_FETCHES = 8

TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)


def strip_html(raw: str) -> str:
    """Page text, roughly. Good enough for sentence-shaped pattern matching.

    Script and style blocks go FIRST. Shopify themes inline their whole catalogue as JSON in a
    <script> tag, and without that removal the JSON becomes "page text": Hedley & Bennett's
    /about stripped to 607,672 chars, inside which a video caption for a COLLABORATOR's brand
    read "Fatima, owner of KOMAL" — matching the `NAME, owner` pattern at full weight and beating
    the real founder page, which says "My name is Ellen Marie Bennett". A wrong name is the one
    outcome this script exists to avoid, so the noise goes before the matching starts.
    """
    raw = re.sub(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", " ", raw)
    raw = TAG_RE.sub(" ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_mod.unescape(raw)
    # Any JSON surviving outside a script tag (data-* attributes, JSON-LD) is not prose and must
    # never supply a candidate name.
    raw = re.sub(r'"[A-Za-z_]+"\s*:\s*"[^"]*"', " ", raw)
    return re.sub(r"[ \t ]+", " ", raw)


def fetch(url: str, timeout: int = 9) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # public HTML only; some DTC certs chain oddly
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(900_000)
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def brand_words(brand: str, domain: str) -> set:
    """Tokens that belong to the company, not a person.

    "Gracie's Doggie Delights" publishes "Gracie" everywhere and it IS the founder's name —
    but "Cape Candle" publishes "Cape", which is not. The brand's own words are dropped
    because we cannot tell those two cases apart from the outside, and the safe error is
    the one that costs a greeting.
    """
    words = set()
    for chunk in (brand or "", (domain or "").split(".")[0]):
        for t in re.split(r"[^A-Za-z]+", chunk):
            if len(t) > 1:
                words.add(t.lower())
    return words


def plausible(name: str, banned: set, first_names: set, weight: int = 0) -> bool:
    """Is this a person's name, rather than a capitalised noun phrase that matched?

    `weight` is the confidence of the pattern that produced it. A bare first name normally
    has to be one we recognise, because "Meet Bella" is as likely to be a dog. But when the
    match itself said "Co-Founders Eileen", the founder phrase IS the evidence and the name
    dictionary is the wrong gate — Eileen, Anouk and Sirisha are not in it and are real.
    """
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    if not toks or len(toks) > 3:
        return False
    low = [re.sub(r"[^a-z]", "", t.lower()) for t in toks]
    if any(not t for t in low):
        return False
    if any(t in STOP for t in low):
        return False
    if any(t in banned for t in low):
        return False
    if len(toks) == 1:
        # A bare first name is only believable if it is one we recognise, OR if an explicit
        # founder/owner phrase introduced it. Otherwise it is far more likely to be a
        # product, a place or a word the page happened to capitalise.
        return low[0] in first_names or (weight >= 4 and 3 <= len(low[0]) <= 14)
    # Two or three tokens: require the first to read as a given name, either by being a
    # known one or by being a short alphabetic token that is not a banned/stop word.
    return low[0] in first_names or (3 <= len(low[0]) <= 12)


# "Created by", "designed by", "built by", "powered by" introduce a VENDOR as often as a founder —
# the web designer, the photographer, the agency. When one of these words sits just before the
# match, it is a credit line, not a founder story.
CREDIT_CONTEXT = re.compile(
    # One optional verb may sit between the noun and "by" — the real case was
    # "Site Created by GoldBear.Media", where `site` and `by` are not adjacent.
    r"(site|website|web|store|theme|design|designed|developed|built|powered|photo|photography|"
    r"branding|logo|template|shopify)\s+(?:\w+\s+)?(by|:)\s*$", re.I)


def is_credit_line(text: str, start: int) -> bool:
    """Is the run-up to this match a 'Site Created by ...' style credit?"""
    return bool(CREDIT_CONTEXT.search(text[max(0, start - 40):start]))


def sentence_around(text: str, idx: int, width: int = 180) -> str:
    start = max(0, idx - width // 2)
    snippet = text[start:idx + width].strip()
    return re.sub(r"\s+", " ", snippet)


ABOUT_HREF = re.compile(
    r'href=["\']([^"\']*(?:about|our-story|ourstory|/story|founder|meet-|who-we-are|philosophy|our-mission|/mission|our-values|our-why|'
    r'our-team|the-team|our-mission)[^"\']*)["\']', re.I)


def about_links(home_html: str, base: str, limit: int = 6) -> list:
    """About-ish paths this site actually links to.

    The fixed PATHS list misses more than it hits: Shopify stores name these pages
    anything they like (/pages/our-story-1, /pages/lsf-team, /pages/from-our-founder)
    and a guessed path just 404s. The homepage nav already names the real ones, so read
    them out of it instead of guessing.
    """
    out, seen = [], set()
    root = base.split("//")[-1].split("/")[0].lower()
    for href in ABOUT_HREF.findall(home_html or ""):
        href = html_mod.unescape(href.strip())
        if href.startswith("http"):
            if root not in href.lower():
                continue                      # an about page on someone else's domain isn't theirs
            path = "/" + href.split("//")[-1].split("/", 1)[-1] if "/" in href.split("//")[-1] else "/"
        elif href.startswith("/"):
            path = href
        else:
            continue
        path = path.split("#")[0].split("?")[0].rstrip("/")
        if not path or path in seen or any(path.lower().endswith(x) for x in
                                           (".jpg", ".png", ".pdf", ".webp", ".svg")):
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def names_for(brand: str, domain: str, first_names: set, verbose=False):
    """Return (name, title, evidence, pages_read) for one brand, or (None, ...) on no verdict."""
    domain = (domain or "").strip().rstrip("/")
    if not domain:
        return None, "", "", 0
    base = domain if domain.startswith("http") else \
        "https://" + domain.replace("https://", "").replace("http://", "")
    banned = brand_words(brand, domain)
    scores, evidence, titles = {}, {}, {}
    pages = 0
    # Read the homepage once up front, both for its own text and for the About links it
    # names, then try those before falling back to the guessed paths.
    paths = list(PATHS)
    try:
        home = fetch(base)
        discovered = about_links(home, base)
        if verbose and discovered:
            print(f"      linked about pages: {', '.join(discovered)}")
        paths = discovered + [p for p in paths if p not in discovered]
    except Exception as e:
        if verbose:
            print(f"      homepage -> {type(e).__name__}")
    # Every ATTEMPT counts against the budget, not just the successes. A failed fetch used to cost
    # nothing, so a site whose first paths 404 got all thirteen tried at 0.3s intervals — which
    # looks like a scanner and earns an HTTP 429. Goldilocks Goods rate-limited us on 2026-09-21
    # and the run reported "no clear founder name (0 pages read)" for a brand that names its
    # founder in plain text on its own about page. A throttled read is an unknown, not a no.
    attempts = 0
    throttled = False
    for path in paths:
        if attempts >= MAX_FETCHES or throttled:
            break
        attempts += 1
        try:
            text = strip_html(fetch(base + path))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                throttled = True
                if verbose:
                    print(f"      {path or '/'} -> HTTP 429, backing off (result is UNKNOWN, not no)")
            elif verbose:
                print(f"      {path or '/'} -> HTTP {e.code}")
            continue
        except Exception as e:
            if verbose:
                print(f"      {path or '/'} -> {type(e).__name__}")
            continue
        pages += 1
        if verbose:
            print(f"      {path or '/'} -> {len(text)} chars")
        if len(text) > MAX_PAGE_CHARS:
            if verbose:
                print(f"      {path or '/'} skipped: {len(text)} chars is a data dump, not prose")
            continue
        for pat, weight in PATTERNS:
            for m in pat.finditer(text):
                cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
                if not plausible(cand, banned, first_names, weight):
                    continue
                if is_credit_line(text, m.start()):
                    continue
                key = cand.lower()
                scores[key] = scores.get(key, 0) + weight
                if key not in evidence:
                    evidence[key] = sentence_around(text, m.start())
                    titles[key] = cand
                    low = m.group(0).lower()
                    for t in ("co-founder", "cofounder", "founder", "owner", "ceo"):
                        if t in low:
                            titles[key] = cand
                            evidence[key + ":title"] = t
                            break
        if pages >= MAX_PAGES:
            # Bounded, but never stopped early just because SOMETHING matched: quitting on the
            # first weak candidate is what made Moon Juice return nothing while its About page
            # said "Amanda Chantal Bacon Founder & CEO" in plain text. Decide on all of it.
            break
        time.sleep(0.6)
    if not scores:
        # "" pages read because we were throttled is a different answer from "read them, found
        # nothing" — and only the second one means the brand publishes no founder.
        return None, ("throttled" if throttled and not pages else ""), "", pages
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_key, top_score = ranked[0]
    # A clear winner, or nothing. Two founders tied is a real and common case (co-founders),
    # and picking one at random is how you greet the wrong half of a couple.
    if top_score < 4:
        return None, "", "", pages
    if len(ranked) > 1 and ranked[1][1] >= top_score:
        return None, "", "", pages
    title = evidence.get(top_key + ":title", "")
    return titles[top_key], title, evidence[top_key], pages


def load_first_names() -> set:
    """Reuse the drafter's own first-name list so the two agree on what a name is."""
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splitframe_daily.py")
        spec = importlib.util.spec_from_file_location("_sfd_names", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return set(mod.FIRST_NAMES)
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=None)
    ap.add_argument("--status", default="qualified")
    ap.add_argument("--write", action="store_true", help="write results back to the tracker")
    ap.add_argument("--refresh", action="store_true", help="re-look-up rows that already have a name")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    first_names = load_first_names()
    with open(TRACKER, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
        fields = list(rd.fieldnames)
    if "contact_name_source" not in fields:
        fields.append("contact_name_source")

    todo = []
    for r in rows:
        r.setdefault("contact_name_source", "")
        if a.brand and (r.get("brand") or "").lower() != a.brand.lower():
            continue
        if not a.brand:
            if a.status and (r.get("status") or "").strip() != a.status:
                continue
            if (r.get("sent_date") or "").strip() or (r.get("replied") or "").strip():
                continue
            if (r.get("outcome") or "").strip():
                continue
            if not ((r.get("email") or "").strip() or (r.get("email_generic") or "").strip()):
                continue
        if (r.get("contact_name") or "").strip() and not a.refresh:
            continue
        todo.append(r)
    if a.limit:
        todo = todo[:a.limit]

    print(f"{len(todo)} brand(s) to look up"
          f"{'' if a.write else '  [DRY RUN — pass --write to save]'}\n")
    hits = 0
    for i, r in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {r['brand'][:26]:<26} {r['domain']}")
        try:
            name, title, ev, pages = names_for(r["brand"], r["domain"], first_names,
                                               verbose=a.verbose)
        except Exception as e:
            print(f"      ERROR {type(e).__name__}: {e}")
            name, title, ev, pages = None, "", "", 0
        if name:
            hits += 1
            print(f"      ✓ {name}" + (f"  ({title})" if title else ""))
            print(f"        “{ev[:150]}”")
            r["contact_name"] = name
            if title and not (r.get("contact_title") or "").strip():
                r["contact_title"] = title.title()
            r["contact_name_source"] = f"{r['domain']} about page, auto {time.strftime('%Y-%m-%d')}: {ev[:200]}"
        elif title == "throttled":
            # We never got to read the site. Recording "no clear founder name" here would write a
            # false fact into the tracker — a human reading the row would believe this brand
            # publishes no founder when nobody has actually looked.
            print(f"      ~ rate-limited (HTTP 429) — UNKNOWN, not a no; retry this brand later")
            r["contact_name_source"] = f"rate-limited, not yet read, auto {time.strftime('%Y-%m-%d')}"
        else:
            print(f"      ✗ no clear founder name ({pages} page(s) read)")
            r["contact_name_source"] = f"no clear founder name, auto {time.strftime('%Y-%m-%d')}"
        time.sleep(a.delay)

    print(f"\nfound {hits}/{len(todo)}")
    if a.write:
        shutil.copy(TRACKER, TRACKER + ".bak-pre-founder-names")
        with open(TRACKER, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"tracker written (backup: {os.path.basename(TRACKER)}.bak-pre-founder-names)")
    else:
        print("dry run — nothing written")


if __name__ == "__main__":
    main()
