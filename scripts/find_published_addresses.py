#!/usr/bin/env python3
"""Find founders' PUBLISHED email addresses on DTC brands' own sites. No paid API.

Why this exists (2026-09-25): the ad-creative lane sends on one rule, "named person or no send"
(NAMED_ONLY in splitframe_daily.py). A first touch goes only to a named founder's own address,
published on the brand's own site or verified. Hunter's free quota is spent, so published
addresses are now the whole supply. Two were found by hand this week, both in privacy policies:
sarah@curiebod.com (curiebod.com privacy policy) and martina@builtbyswift.com
(builtbyswift.com/policies/privacy-policy). A small brand's privacy policy, terms and Shopify
/policies pages often name one real person as the data contact, which a footer never does.
This reads those pages for every brand still waiting on a named address.

What it reads: the brand's own domain only. That means the privacy/terms/about/press/wholesale/
contact paths, the homepage, a couple of about links the homepage names, and Shopify's
/pages.json and /policies/* when they respond. It picks up plain addresses, mailto: links,
HTML-entity and Cloudflare-obfuscated addresses, "name [at] domain [dot] com" forms, and JSON-LD.
It never guesses or builds an address: a guessed address bounces, and a bounce costs the
sending reputation.

What counts. An address is only kept if it sits on the brand's domain (or a subdomain of it) and
reads as a PERSON:
    named       the local part matches a name. That is the tracker's contact_name, a founder
                named on the site ("founded by X", "X, Owner"), or a name printed beside the
                address.
    first_name  the local part is a known first name, or first.last. Nobody on the page
                confirms who it is.
Generic inboxes (info/hello/support/press/sales/orders/wholesale/privacy and the rest of
splitframe_daily's ticket and front-desk sets) are NOT results. Neither are brand-voice
inboxes like justdoughit@ or the brand's own name, because under NAMED_ONLY nobody can be
greeted there.

The tracker is READ-ONLY here. Results go to a dated side file in the vault's Money folder, and a
person moves any address into the tracker by hand after reading the evidence.

Usage:
    python3 scripts/find_published_addresses.py --dry-run --limit 5     # read, print, write nothing
    python3 scripts/find_published_addresses.py --limit 60
    python3 scripts/find_published_addresses.py --only curiebod.com --dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_mod
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import NamedTuple, Optional
from urllib.parse import unquote, urlsplit

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import find_founder_names as ff  # noqa: E402  strip_html, founder PATTERNS, plausible, brand_words
import splitframe_daily as sfd  # noqa: E402   the send gate's own idea of a person vs an inbox

VAULT = os.environ.get("VAULT_PATH") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
OUT_DIR = os.path.join(VAULT, "Money")
VAULT_GIT = os.path.expanduser("~/.second-brain-vault.git")

UA = ff.UA
TIMEOUT = 15             # seconds per request
MIN_GAP = 1.0            # polite: at most one request per second to any one domain
RETRY_WAIT = 5.0         # the pause before the single retry after a 403/429
MAX_RETRY_WAIT = 20.0    # a Retry-After longer than this is treated as "skip it"
BLOCK_STREAK = 3         # consecutive refused paths before we leave a domain alone
MAX_BYTES = 3_000_000
WORKERS = 6              # different domains in parallel; one domain is always sequential
EXTRA_ABOUT_LINKS = 2    # about pages the homepage links to, beyond the fixed paths

# Homepage first, because it settles the real origin (redirects) and names the about links.
# /pages.json comes next: on Shopify it returns every page's body in one request.
PATHS = ["", "/pages.json?limit=250",
         "/policies/privacy-policy", "/pages/privacy-policy", "/privacy", "/privacy-policy",
         "/policies/terms-of-service", "/pages/terms",
         "/pages/about", "/pages/about-us", "/pages/our-story", "/pages/press",
         "/pages/wholesale", "/pages/contact",
         "/policies/contact-information", "/policies/legal-notice"]

COLUMNS = ["brand", "domain", "address", "page_url", "evidence_snippet",
           "person_name_if_shown", "found_at", "tier"]

# Where a redirect can land that is NOT the brand's own domain, so it never widens the domain set.
PLATFORM_HOSTS = ("myshopify.com", "shopify.com", "shop.app", "linktr.ee", "wixsite.com",
                  "squarespace.com", "bigcartel.com", "etsy.com", "amazon.com", "instagram.com",
                  "facebook.com", "godaddysites.com", "webflow.io", "tiktok.com")
# Tracker rows whose "domain" is a creator channel, not a brand site. The creator lane is exempt
# from NAMED_ONLY anyway.
NOT_BRAND_SITES = ("twitch.tv", "youtube.com", "kick.com", "instagram.com", "tiktok.com")

# --------------------------------------------------------------------------- what an inbox is

# The brief's own list plus the send gate's ticket and front-desk sets, and the policy-page
# inboxes (privacy@, dpo@, legal@) that a privacy page is full of.
GENERIC = set(sfd.TICKET_LOCALS) | set(sfd.FRONT_LOCALS) | {
    "info", "hello", "support", "press", "sales", "orders", "wholesale",
    "privacy", "legal", "dpo", "gdpr", "ccpa", "compliance", "dataprotection", "data",
    "careers", "jobs", "hr", "recruiting", "hiring", "accessibility", "ada", "feedback",
    "affiliate", "affiliates", "ambassador", "ambassadors", "collab", "collabs", "collaborations",
    "influencer", "influencers", "creators", "creator", "events", "donations", "donate", "giving",
    "community", "retail", "b2b", "trade", "webmaster", "web", "exchanges", "shipping",
    "customercare", "customer", "customers", "cx", "finance", "accounting", "ap", "ar", "invoices",
    "invoice", "pr", "social", "reviews", "rewards", "subscriptions", "subscribe", "newsletter",
    "unsubscribe", "ops", "operations", "fulfillment", "warehouse", "logistics", "sponsorships",
    "sponsor", "hey", "yo", "howdy", "questions", "question", "inquiry", "enquiries", "enquiry",
    "hola", "bonjour", "ciao", "love", "us", "we", "hq", "corporate", "investors", "ir",
    "business", "bizdev", "bd", "partnership", "licensing", "refunds", "refund", "security",
    "abuse", "postmaster", "hostmaster", "root", "test", "demo", "example", "user", "name",
    "you", "yourname", "firstname", "lastname", "jobs", "concierge", "stylist", "stockists",
    "stockist", "bulk", "gifting", "gifts", "corporategifts", "catering", "reservations",
    "bookings", "booking", "appointments", "vip", "members", "membership", "loyalty",
    # Regional desks: "Europe Hours of Operation ... europe@manduka.com" read as a person named
    # Europe Hours on the first real run (2026-09-25).
    "europe", "eu", "uk", "usa", "us", "canada", "ca", "au", "australia", "asia", "emea", "apac",
    "latam", "intl", "international", "global", "na", "northamerica",
}
# A local part CONTAINING one of these words is still an inbox (customer.service, hello.us,
# wholesale-orders), not a person.
GENERIC_TOKENS = {"info", "hello", "support", "help", "sales", "orders", "order", "wholesale",
                  "press", "privacy", "service", "care", "contact", "customer", "customers",
                  "team", "returns", "shipping", "careers", "jobs", "noreply", "admin",
                  "billing", "accounts", "marketing", "media", "partnerships", "legal"}
# Role words that mark an inbox wherever they sit in the local part (yogisupport@, shoporders@).
GENERIC_SUBSTRINGS = ("support", "service", "orders", "wholesale", "customer", "privacy",
                      "careers", "returns", "shipping", "marketing", "partnership", "noreply",
                      "no-reply", "donotreply", "inquiries", "enquiries")
GENERIC_PREFIXES = ("info", "hello", "support", "customer", "wholesale", "press", "sales",
                    "order", "privacy", "contact", "service", "careers", "noreply", "no-reply",
                    "donotreply", "return", "shipping", "partner", "marketing", "admin",
                    "billing", "account")

# Founder first names the send gate's list does not carry. A miss here only costs the
# first_name tier; the named tier comes from evidence on the page, not from this list.
EXTRA_FIRST_NAMES = {
    "martina", "irene", "ismail", "ariana", "aisha", "alana", "alejandra", "alessandra", "alina",
    "alyssa", "amelia", "amir", "anastasia", "andre", "angelica", "anika", "anita", "anja",
    "ankit", "anouk", "antonio", "arjun", "astrid", "aubrey", "audrey", "ayesha", "barbara",
    "bea", "beatrice", "bella", "betsy", "bianca", "brianna", "bridget", "camila", "carmen",
    "cassie", "cecilia", "celeste", "celine", "chiara", "christa", "claudia", "colleen", "cora",
    "daisy", "daphne", "darcy", "delia", "desiree", "dina", "dora", "eileen", "elisa", "eliza",
    "elle", "eloise", "elsa", "esther", "evelyn", "farah", "fatima", "flora", "francesca",
    "frances", "gabrielle", "georgia", "gianna", "gloria", "greta", "hana", "harper", "hilary",
    "ines", "ingrid", "isla", "jana", "janelle", "jasmine", "jenn", "jessie", "jocelyn",
    "johanna", "josephine", "joanna", "jolene", "juliana", "juliet", "kara", "karina", "kat",
    "kathleen", "kendra", "kerry", "kira", "kirsten", "kristy", "lana", "laila", "layla",
    "leila", "lena", "lila", "lina", "liza", "lola", "lorena", "lorraine", "louise", "lucia",
    "luisa", "mabel", "mackenzie", "maddie", "mallory", "mara", "marcela", "margo", "margot",
    "marina", "marisa", "marla", "marlene", "matilda", "maxine", "melody", "mika", "mila",
    "mina", "mira", "miriam", "nadia", "nadine", "nava", "nell", "nia", "nikki", "noelle",
    "odette", "olga", "paige", "paloma", "paola", "patty", "penelope", "piper", "priya",
    "quinn", "raquel", "reese", "rhonda", "rita", "roberta", "romy", "rosie", "roxanne",
    "sadie", "salma", "samira", "sasha", "savannah", "selena", "shana", "shira", "silvia",
    "simone", "sonia", "tabitha", "talia", "tamara", "tatiana", "tessa", "thea", "tori",
    "trisha", "vivian", "wanda", "yasmin", "yvonne", "zara", "zoey", "iskra", "veronika",
    "aishwarya", "aidan", "ahmad", "ahmed", "alberto", "alec", "alfredo", "amit", "andres",
    "angelo", "antoine", "armando", "arthur", "axel", "barry", "beau", "bennett", "bernard",
    "brady", "brock", "bryce", "caleb", "calvin", "carter", "cesar", "clay", "clayton", "cole",
    "conor", "dale", "damian", "damon", "dane", "dante", "darius", "darryl", "declan",
    "desmond", "diego", "dmitri", "duncan", "dustin", "elias", "elliot", "elliott", "emmanuel",
    "enrique", "ernest", "ezra", "fabian", "fernando", "finn", "francis", "francisco",
    "franklin", "garrett", "gideon", "gilbert", "glenn", "gordon", "griffin", "guillermo",
    "gus", "hank", "harold", "harrison", "hassan", "hector", "howard", "hugh", "hugo",
    "ibrahim", "igor", "isaiah", "jared", "javier", "jeremiah", "jerome", "joaquin", "jorge",
    "julio", "kai", "karl", "kent", "kirk", "kurt", "landon", "lars", "leon", "leonard",
    "lewis", "lincoln", "lloyd", "lorenzo", "luis", "malcolm", "manuel", "marco", "matteo",
    "maurice", "micah", "miguel", "miles", "milo", "mitch", "mitchell", "mohammed", "muhammad",
    "nico", "nolan", "omar", "otto", "pablo", "pedro", "pierre", "preston", "rafael", "raj",
    "ramon", "raul", "reid", "ricardo", "roberto", "rocco", "rohan", "roman", "rory", "ruben",
    "rudy", "salvador", "santiago", "sergio", "silas", "spencer", "stan", "stanley", "stefan",
    "tanner", "terrence", "theo", "theodore", "troy", "tucker", "vince", "warren", "wesley",
    "xavier", "yusuf", "zane", "matty",
}


def _letters(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def _tokens(local: str) -> list:
    return [t for t in re.split(r"[._+\-\d]+", (local or "").lower()) if t]


def load_first_names() -> set:
    names = set(sfd.FIRST_NAMES) | EXTRA_FIRST_NAMES
    try:   # macOS ships ~1,300 given names; absent on the server, which is fine
        with open("/usr/share/dict/propernames", encoding="utf-8") as f:
            names |= {_letters(line) for line in f if len(_letters(line)) >= 3}
    except OSError:
        pass
    return names


FIRST_NAMES = load_first_names()


def is_generic(local: str) -> bool:
    """An inbox, not a person: info@, customer.service@, hello-us@, sup@."""
    local = (local or "").lower().strip()
    if not local:
        return True
    if local in GENERIC or _letters(local) in GENERIC:
        return True
    if any(t in GENERIC_TOKENS for t in _tokens(local)):
        return True
    if any(w in local for w in GENERIC_SUBSTRINGS):
        return True
    return local.startswith(GENERIC_PREFIXES)


def is_first_name(local: str) -> bool:
    """Person-shaped with no confirmation: a known first name, or first.last."""
    local = (local or "").lower()
    return _letters(local) in FIRST_NAMES or bool(sfd.DOTTED_NAME.match(local))


def is_brand_token(local: str, brand_words: set) -> bool:
    """gracie@ at Gracie's Doggie Delights, oudwarellc@ at Oudware: the company's own name."""
    letters = _letters(local)
    if not letters:
        return False
    if letters in brand_words:
        return True
    return any(len(w) >= 4 and w in letters for w in brand_words)


def name_matches_local(local: str, name: str) -> bool:
    """Does the local part spell this person? sarah, sarahkaplan, skaplan, sarahk, kaplan.
    A bare initial never counts from a page; only the tracker's own contact gets that
    (sfd.matches_contact), because j@ on its own could be anybody."""
    parts = [t.lower() for t in re.split(r"[^A-Za-z]+", name or "") if len(t) > 1]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    forms = {first, first + last, last + first, first[0] + last, first + last[0]}
    if len(parts) > 1:
        forms.add(last)
    return _letters(local) in forms


# --------------------------------------------------------------------------- domains

def norm_domain(value: str) -> str:
    d = (value or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".")
    return d[4:] if d.startswith("www.") else d


def on_domain(addr_domain: str, domains) -> bool:
    """The brand's domain or a subdomain of it. Never a lookalike: support@x.freshdesk.com is
    Freshdesk's, and sarah@curiecosmetics.com is a different domain from curiebod.com."""
    a = (addr_domain or "").lower().rstrip(".")
    return any(a == d or a.endswith("." + d) for d in domains if d)


def is_platform_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


# --------------------------------------------------------------------------- extraction

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})@"
    r"([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24})(?![A-Za-z0-9\-])")
# "jane [at] brand [dot] com", "jane(at)brand.com", "jane {at} brand . com"
_BR_AT = r"\s*[\[\(\{<]\s*(?:at|@)\s*[\]\)\}>]\s*"
_BR_DOT = r"\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*"
_WORD_DOT = r"\s+dot\s+"
_LABEL = r"[A-Za-z0-9\-]+"
OBF_BRACKET_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]*)" + _BR_AT +
    r"(" + _LABEL + r"(?:(?:\.|" + _BR_DOT + r"|" + _WORD_DOT + r")" + _LABEL + r")+)", re.I)
# "jane at brand dot com". A bare " at " is only trusted when the dot is spelled out too:
# "chat with Sarah at curiebod.com" is a sentence about a website, and reading it as
# sarah@curiebod.com would be a constructed address.
OBF_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]*)\s+at\s+"
    r"(" + _LABEL + r"(?:(?:" + _BR_DOT + r"|" + _WORD_DOT + r")" + _LABEL + r")+)", re.I)
_DOT_NORM = re.compile(r"\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*|\s+dot\s+", re.I)

MAILTO_RE = re.compile(r"""href\s*=\s*["']\s*mailto:([^"'?#]+)""", re.I)
CF_SPAN_RE = re.compile(
    r"""<(span|a)\b[^>]*data-cfemail\s*=\s*["']([0-9a-fA-F]+)["'][^>]*>.*?</\1\s*>""", re.I | re.S)
CF_HREF_RE = re.compile(r"""/cdn-cgi/l/email-protection#([0-9a-fA-F]+)""", re.I)
LD_JSON_RE = re.compile(
    r"""<script\b[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script\s*>""", re.I | re.S)


def cf_decode(hexstr: str) -> str:
    """Cloudflare's email obfuscation: the first byte is an XOR key for the rest."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def _clean_address(local: str, dom: str, domains) -> Optional[str]:
    # "u003einfo@": a JSON-escaped ">" whose backslash was lost on the way into page text.
    local = re.sub(r"^(?:u003[ce])+", "", local.strip(".-_+"), flags=re.I).lower()
    labels = dom.strip(".").split(".")
    # "sarah@curiebod.com.Our next section": a tag boundary glued the next sentence on.
    # A capitalised last label is prose, not a TLD, so it comes off before the domain check.
    while len(labels) > 2 and labels[-1][:1].isupper() and not on_domain(".".join(labels), domains):
        labels = labels[:-1]
    d = ".".join(labels).lower()
    if not local or not d:
        return None
    return f"{local}@{d}"


def _snippet(text: str, start: int, end: int, width: int = 110) -> str:
    s = text[max(0, start - width):min(len(text), end + width)]
    return re.sub(r"\s+", " ", s).strip()[:260]


def _prep_html(raw: str) -> str:
    """Turn Cloudflare-protected addresses back into text before anything else reads the page."""
    raw = CF_SPAN_RE.sub(lambda m: " " + cf_decode(m.group(2)) + " ", raw or "")
    return CF_HREF_RE.sub(lambda m: "mailto:" + cf_decode(m.group(1)), raw)


class Hit(NamedTuple):
    address: str
    page_url: str
    snippet: str
    window: str      # a wider slice of page text around the address, for names printed beside it
    how: str         # text | obfuscated | mailto | json-ld


def extract(raw_html: str, page_url: str, domains) -> tuple:
    """(page_text, hits, off_domain_count) for one page. Addresses off the brand's domain are
    counted and dropped here, so nothing downstream can promote one."""
    raw = _prep_html(raw_html)
    text = ff.strip_html(raw)
    # Theme data that survives the scrub carries \u003e / \u0040 escapes; decode them so an
    # escaped address reads as the address it is.
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    hits, off = [], 0
    seen = set()

    def add(local, dom, start, end, how, src_text):
        nonlocal off
        addr = _clean_address(local, dom, domains)
        if not addr:
            return
        if not on_domain(addr.split("@", 1)[1], domains):
            off += 1
            return
        key = (addr, how)
        if key in seen:
            return
        seen.add(key)
        hits.append(Hit(addr, page_url, _snippet(src_text, start, end),
                        src_text[max(0, start - 250):end + 250], how))

    for m in EMAIL_RE.finditer(text):
        add(m.group(1), m.group(2), m.start(), m.end(), "text", text)
    for rx in (OBF_BRACKET_RE, OBF_WORD_RE):
        for m in rx.finditer(text):
            dom = _DOT_NORM.sub(".", m.group(2))
            if "." not in dom:
                continue
            add(m.group(1), dom, m.start(), m.end(), "obfuscated", text)
    for m in MAILTO_RE.finditer(raw):
        for part in unquote(html_mod.unescape(m.group(1))).split(","):
            em = EMAIL_RE.search(part.strip())
            if not em:
                continue
            addr = f"{em.group(1)}@{em.group(2)}".lower()
            pos = text.lower().find(addr)
            if pos >= 0:     # printed on the page too: the text hit already carries the context
                add(em.group(1), em.group(2), pos, pos + len(addr), "text", text)
            else:
                label = ff.strip_html(raw[m.end():m.end() + 300].split("</a", 1)[0].split(">", 1)[-1])
                label = re.sub(r"\s+", " ", label).strip()[:80] or "no link text"
                ctx = f"mailto link: {addr} ({label})"
                add(em.group(1), em.group(2), 0, len(ctx), "mailto", ctx)
    for m in LD_JSON_RE.finditer(raw):
        blob = html_mod.unescape(m.group(1))
        for em in EMAIL_RE.finditer(blob):
            add(em.group(1), em.group(2), em.start(), em.end(), "json-ld", blob)
    return text, hits, off


def founder_names(text: str, banned: set) -> list:
    """Names the site itself calls founder/owner/CEO, via find_founder_names' weighted
    patterns (weight >= 4 only: 'Meet X' is as likely to be a dog)."""
    if not text or len(text) > ff.MAX_PAGE_CHARS:
        return []
    out = []
    for pat, weight in ff.PATTERNS:
        if weight < 4:
            continue
        for m in pat.finditer(text):
            cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
            if ff.plausible(cand, banned, FIRST_NAMES, weight) and not ff.is_credit_line(text, m.start()):
                out.append((cand, ff.sentence_around(text, m.start())))
    return out


NAME_RE = re.compile(ff.NAME)


def name_beside(hit: Hit, banned: set) -> Optional[str]:
    """A person's name printed within ~250 characters of the address, spelling its local part:
    'Questions? Email Martina Keller at martina@...' -> 'Martina Keller'.

    The name has to START with a known first name. Beside an address, a capitalised phrase is
    usually a heading, not a person: 'Europe Hours of Operation' next to europe@manduka.com
    was the first real run's one false 'named' hit."""
    local = hit.address.split("@", 1)[0]
    best = None
    for m in NAME_RE.finditer(hit.window):
        cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
        cand = re.sub(r"['\u2019]s$", "", cand)          # "Martina's" -> "Martina"
        toks = cand.split()
        # A multi-word match can swallow a capitalised word in front ("Email Martina Keller"),
        # so try every tail of it, longest first.
        for i in range(len(toks)):
            sub = " ".join(toks[i:])
            if not name_matches_local(local, sub):
                continue
            if _letters(sub.split()[0]) not in FIRST_NAMES:
                continue
            if not ff.plausible(sub, banned, FIRST_NAMES, 4):
                continue
            if best is None or len(sub.split()) > len(best.split()):
                best = sub
            break
    return best


# --------------------------------------------------------------------------- fetching

class Page(NamedTuple):
    status: int            # 0 = could not connect
    url: str               # final URL after redirects
    text: str
    ctype: str
    retry_after: Optional[float] = None


_now = time.monotonic
_sleep = time.sleep
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA,
                          "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                          "Accept-Language": "en-US,en;q=0.9"})
        _local.session = s
    return s


def http_get(url: str) -> Page:
    """One GET. The only network call in this module, so tests replace just this."""
    def _go(verify):
        r = _session().get(url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=verify)
        try:
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = b""
            if r.status_code == 200 and any(t in ctype for t in ("html", "json", "text", "xml")):
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
            ra = r.headers.get("Retry-After")
            try:
                ra = float(ra) if ra else None
            except ValueError:
                ra = None
            # requests assumes latin-1 for text/html without a charset, which garbles every
            # curly apostrophe in a founder's name. Header charset, else UTF-8, else latin-1.
            m = re.search(r"charset=([\w\-]+)", ctype)
            try:
                text = body.decode(m.group(1) if m else "utf-8")
            except (UnicodeDecodeError, LookupError):
                text = body.decode("latin-1", errors="replace")
            return Page(r.status_code, r.url, text, ctype, ra)
        finally:
            r.close()
    try:
        return _go(True)
    except requests.exceptions.SSLError:
        try:   # public HTML only; some small-brand certificates chain oddly
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            return _go(False)
        except requests.RequestException as e:
            return Page(0, url, str(e)[:200], "")
    except requests.RequestException as e:
        return Page(0, url, str(e)[:200], "")


class DomainClient:
    """Sequential, polite fetching for ONE domain: >= MIN_GAP between request starts, one
    retry after a 403/429, and a stop after BLOCK_STREAK refusals in a row."""

    def __init__(self):
        self.last = None
        self.requests = 0
        self.refused_streak = 0
        self.refused = 0
        self.gave_up = False

    def _turn(self):
        if self.last is not None:
            wait = MIN_GAP - (_now() - self.last)
            if wait > 0:
                _sleep(wait)
        self.last = _now()
        self.requests += 1

    def get(self, url: str) -> Optional[Page]:
        if self.gave_up:
            return None
        page = None
        for attempt in (1, 2):
            self._turn()
            page = http_get(url)
            if page.status not in (403, 429):
                self.refused_streak = 0
                return page
            if attempt == 1:
                wait = page.retry_after if page.retry_after is not None else RETRY_WAIT
                if wait > MAX_RETRY_WAIT:
                    break
                _sleep(max(wait, MIN_GAP))
        self.refused += 1
        self.refused_streak += 1
        if self.refused_streak >= BLOCK_STREAK:
            self.gave_up = True
        return None


# --------------------------------------------------------------------------- one brand

def assess(address: str, hits: list, row: dict, site_founders: list, site_text: str,
           banned: set) -> tuple:
    """(tier, basis, person_name, best_hit) or (None, reason, "", None)."""
    local = address.split("@", 1)[0]
    if is_generic(local):
        return None, "generic", "", None
    brandish = is_brand_token(local, banned)
    contact = (row.get("contact_name") or "").strip()
    if contact and sfd.matches_contact(address, contact):
        first = contact.split()[0]
        shown = contact if re.search(r"\b" + re.escape(first) + r"\b", site_text or "") else ""
        return "named", "tracker contact_name", shown, hits[0]
    for name, _ev in site_founders:
        if name_matches_local(local, name):
            return "named", "founder named on the site", name, hits[0]
    if not brandish:
        for h in hits:
            nb = name_beside(h, banned)
            if nb:
                return "named", "name printed beside the address", nb, h
        if is_first_name(local):
            return "first_name", "first name only", "", hits[0]
    return None, "not a person", "", None


def scan_domain(row: dict, verbose: bool = False) -> dict:
    brand = (row.get("brand") or "").strip()
    domain = norm_domain(row.get("domain"))
    res = {"brand": brand, "domain": domain, "pages": 0, "requests": 0, "status": "ok",
           "results": [], "excluded": Counter(), "excluded_addresses": [], "log": []}
    client = DomainClient()
    domains = {domain}
    banned = ff.brand_words(brand, domain)

    base = None
    home_html = ""
    for cand in (f"https://{domain}", f"https://www.{domain}"):
        page = client.get(cand + "/")
        if page is None:
            break
        if page.status == 0:
            res["log"].append(f"{cand}/ -> unreachable ({page.text[:60]})")
            continue
        final = urlsplit(page.url or cand)
        host = norm_domain(final.hostname or "")
        if host and host != domain and not is_platform_host(host):
            domains.add(host)                      # brand.com -> brandco.com: same brand
        if final.hostname and not is_platform_host(final.hostname):
            base = f"{final.scheme}://{final.hostname}"
        else:
            base = cand
        if page.status == 200:
            home_html = page.text
        break
    if base is None:
        res["status"] = "blocked" if client.gave_up or client.refused else "unreachable"
        res["requests"] = client.requests
        return res

    paths = list(PATHS)
    for p in ff.about_links(home_html, base, limit=6):
        if p not in paths and len(paths) < len(PATHS) + EXTRA_ABOUT_LINKS:
            paths.append(p)

    pages = []           # (url, raw_html)
    if home_html:
        pages.append((base + "/", home_html))
    home_hash = hashlib.sha1(home_html.encode("utf-8", "replace")).hexdigest() if home_html else ""
    seen_hashes = {home_hash} if home_hash else set()
    for path in paths[1:]:
        page = client.get(base + path)
        if client.gave_up:
            res["log"].append(f"gave up after {BLOCK_STREAK} refusals in a row")
            break
        if page is None or page.status != 200 or not page.text:
            continue
        if path.startswith("/pages.json"):
            try:
                data = json.loads(page.text)
            except ValueError:
                continue
            for pg in ((data.get("pages") or []) if isinstance(data, dict) else [])[:250]:
                if not isinstance(pg, dict):
                    continue
                body = pg.get("body_html") or ""
                if body and pg.get("handle"):
                    pages.append((f"{base}/pages/{pg['handle']}", body))
            continue
        h = hashlib.sha1(page.text.encode("utf-8", "replace")).hexdigest()
        if h in seen_hashes:      # a single-page app answering every path with the homepage
            continue
        seen_hashes.add(h)
        pages.append((page.url or base + path, page.text))

    res["pages"] = len(pages)
    res["requests"] = client.requests
    if client.gave_up and len(pages) <= 1:
        res["status"] = "blocked"

    by_addr = {}
    texts = []
    founders = []
    for url, raw in pages:
        text, hits, off = extract(raw, url, domains)
        texts.append(text)
        founders.extend(founder_names(text, banned))
        res["excluded"]["off-domain"] += off
        for h in hits:
            by_addr.setdefault(h.address, []).append(h)
    site_text = "\n".join(texts)
    found_at = datetime.now().isoformat(timespec="seconds")
    for addr, hits in by_addr.items():
        tier, basis, person, best = assess(addr, hits, row, founders, site_text, banned)
        if tier is None:
            res["excluded"][basis] += 1
            res["excluded_addresses"].append((addr, basis, hits[0].page_url, hits[0].snippet))
            continue
        res["results"].append({
            "brand": brand, "domain": domain, "address": addr, "page_url": best.page_url,
            "evidence_snippet": best.snippet, "person_name_if_shown": person,
            "found_at": found_at, "tier": f"{tier} ({basis})" if tier == "named" else tier,
        })
    res["results"].sort(key=lambda r: (not r["tier"].startswith("named"), r["address"]))
    return res


# --------------------------------------------------------------------------- tracker (read-only)

def _run(cmd: list):
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def mirror_text() -> Optional[str]:
    try:
        r = subprocess.run(["git", "--git-dir", VAULT_GIT, "show", "HEAD:Money/prospect-tracker.csv"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def load_tracker(path: str = None) -> tuple:
    """(rows, source). An iCloud-evicted tracker reads as empty or raises OSError: ask iCloud for
    it, wait, retry once, then fall back to the vault's git mirror. Opened for reading only."""
    p = path or TRACKER
    for attempt in (1, 2):
        try:
            with open(p, "r", newline="", encoding="utf-8-sig") as f:
                text = f.read()
            if text.strip():
                return list(csv.DictReader(io.StringIO(text))), "icloud"
        except OSError:
            pass
        if attempt == 1:
            _run(["brctl", "download", p])
            _sleep(10)
    text = mirror_text()
    if text:
        return list(csv.DictReader(io.StringIO(text.lstrip("﻿")))), "git mirror"
    raise SystemExit(f"tracker unreadable at {p} and no git-mirror copy")


def _s(v) -> str:
    return (v or "").strip()


def eligible(row: dict) -> bool:
    """Qualified, a real brand site, and no named address yet: either no email at all, or only a
    generic front desk that has not been sent to."""
    if _s(row.get("status")).lower() != "qualified":
        return False
    raw = _s(row.get("domain")).lower()
    dom = norm_domain(raw)
    if not dom or "." not in dom or "/" in re.sub(r"^[a-z]+://", "", raw).rstrip("/"):
        return False
    if any(dom == h or dom.endswith("." + h) for h in NOT_BRAND_SITES):
        return False
    email = _s(row.get("email")).lower()
    if not email:
        return True
    local = email.split("@", 1)[0]
    sent = _s(row.get("email_status")).lower() == "sent" or bool(_s(row.get("sent_date")))
    return is_generic(local) and not sent


def select_rows(rows: list, only: str = None, limit: int = 0) -> list:
    if only:
        want = norm_domain(only)
        picked = [r for r in rows if norm_domain(r.get("domain")) == want][:1]
        return picked or [{"brand": want, "domain": want}]
    todo, seen = [], set()
    # Unsent brands first: a founder address is a first touch there, while a brand whose front
    # desk was already written to is a follow-up question.
    for r in sorted((r for r in rows if eligible(r)), key=lambda r: bool(_s(r.get("sent_date")))):
        d = norm_domain(r.get("domain"))
        if d in seen:
            continue
        seen.add(d)
        todo.append(r)
    return todo[:limit] if limit else todo


# --------------------------------------------------------------------------- output (side file)

def default_out() -> str:
    return os.path.join(OUT_DIR, f"Published Addresses — {date.today().isoformat()}.csv")


def write_results(out_path: str, results: list, scanned_domains=()) -> int:
    """Merge into the day's side file and write it atomically. Never the tracker.

    A re-run with --only must not wipe the other brands' finds, so rows are merged by address.
    But for a brand that WAS re-scanned, this run's verdict replaces the old rows: otherwise a
    false hit fixed in the code would live on in the file forever. An address that survives
    keeps its first found_at."""
    tracker = os.path.realpath(TRACKER)
    if os.path.realpath(out_path) == tracker or os.path.basename(out_path) == os.path.basename(TRACKER):
        raise SystemExit("refusing to write: the output path is the prospect tracker")
    merged = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if r.get("address"):
                        merged[r["address"].lower()] = {c: r.get(c, "") for c in COLUMNS}
        except OSError:
            pass
    rescanned = {norm_domain(d) for d in scanned_domains}
    earlier = {k: v.get("found_at", "") for k, v in merged.items()}
    merged = {k: v for k, v in merged.items() if norm_domain(v.get("domain")) not in rescanned}
    for r in results:
        key = r["address"].lower()
        row = {c: r.get(c, "") for c in COLUMNS}
        if earlier.get(key):
            row["found_at"] = earlier[key]
        merged[key] = row
    rows = sorted(merged.values(),
                  key=lambda r: (not r["tier"].startswith("named"), r["brand"].lower(), r["address"]))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, out_path)
    return len(rows)


def summary_line(scans: list, wrote: Optional[int], out_path: str) -> str:
    found = [r for s in scans for r in s["results"]]
    named = sum(1 for r in found if r["tier"].startswith("named"))
    exc = Counter()
    for s in scans:
        exc.update(s["excluded"])
    status = Counter(s["status"] for s in scans)
    brands_hit = len({r["domain"] for r in found})
    tail = (f"wrote {wrote} rows -> {out_path}" if wrote is not None
            else "dry run: nothing written")
    return (f"SUMMARY scanned {len(scans)} brands ({sum(s['pages'] for s in scans)} pages, "
            f"{sum(s['requests'] for s in scans)} requests, {status.get('blocked', 0)} blocked, "
            f"{status.get('unreachable', 0)} unreachable, {status.get('error', 0)} errors) | "
            f"{len(found)} personal addresses on "
            f"{brands_hit} brands ({named} named, {len(found) - named} first-name only) | excluded: "
            f"{exc.get('generic', 0)} generic, {exc.get('not a person', 0)} not-a-person, "
            f"{exc.get('off-domain', 0)} off-domain | {tail}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=0, help="scan at most N brands")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, write nothing")
    ap.add_argument("--only", default=None, help="scan just this domain (eligible or not)")
    ap.add_argument("--out", default=None, help="side-file path (default: dated file in Money/)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    rows, source = load_tracker()
    todo = select_rows(rows, only=a.only, limit=a.limit)
    out_path = a.out or default_out()
    print(f"{len(todo)} brand(s) to scan (tracker read from {source}; read-only)"
          f"{'  [DRY RUN]' if a.dry_run else ''}\n")

    scans = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = {pool.submit(scan_domain, r, a.verbose): r for r in todo}
        for fut in as_completed(futs):
            r = futs[fut]
            try:
                s = fut.result()
            except Exception as e:     # one broken site must not end the run
                s = {"brand": r.get("brand", ""), "domain": norm_domain(r.get("domain")),
                     "pages": 0, "requests": 0, "status": "error", "results": [],
                     "excluded": Counter(), "excluded_addresses": [],
                     "log": [f"{type(e).__name__}: {e}"]}
            scans.append(s)
            with lock:
                n = len(scans)
                found = ", ".join(f"{x['address']} [{x['tier'].split(' ')[0]}]" for x in s["results"])
                print(f"[{n}/{len(todo)}] {s['brand'][:28]:<28} {s['domain']:<30} "
                      f"{s['status']:<11} pages={s['pages']:<3} {found or '-'}")
                if a.verbose:
                    for line in s["log"]:
                        print(f"      {line}")
                    for addr, why, url, snip in s.get("excluded_addresses", []):
                        print(f"      skipped {addr} ({why})  {url}  “{snip[:140]}”")

    results = [r for s in scans for r in s["results"]]
    results.sort(key=lambda r: (not r["tier"].startswith("named"), r["brand"].lower()))
    if results:
        print("\nFOUND")
        for r in results:
            who = f" — {r['person_name_if_shown']}" if r["person_name_if_shown"] else ""
            print(f"  {r['address']:<36} {r['tier']}{who}\n      {r['page_url']}\n"
                  f"      “{r['evidence_snippet'][:200]}”")
    wrote = None if a.dry_run else write_results(out_path, results,
                                                 [s["domain"] for s in scans if s["status"] == "ok"])
    print("\n" + summary_line(scans, wrote, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
