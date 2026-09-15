#!/usr/bin/env python3
"""Fill in the `email` column of the prospect tracker from brands' own public sites.

Why this exists: on 2026-08-30 we found the real reason Wave 1 never sent — the
tracker had no email column and no addresses, so "send Wave 1 at 11:15" was never
actually executable. This closes that hole permanently, and re-runs cheaply for
every future wave.

It reads ONLY public pages on the brand's own domain (the homepage footer first,
then the usual contact paths) and pulls published addresses. It does not guess or
construct addresses — a guessed address bounces, and bounces damage the sending
reputation that took three weeks of warmup to build.

Addresses are ranked, because who you reach matters more than reaching someone:
    partnerships/press/marketing  > hello/info/contact > help/support/care
A support desk can only delete a pitch; a press or partnerships inbox can route it.

Usage:
    python3 scripts/find_prospect_emails.py --status qualified       # dry run
    python3 scripts/find_prospect_emails.py --status qualified --write
    python3 scripts/find_prospect_emails.py --wave 2 --write
    python3 scripts/find_prospect_emails.py --brand "Wild One" --write
"""
import argparse
import csv
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request

TRACKER = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain/"
    "Money/prospect-tracker.csv")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")

# Homepage first: DTC footers carry the contact address far more often than a
# contact page does (which is usually just a form).
PATHS = ["", "/pages/contact", "/pages/contact-us", "/contact", "/contact-us",
         "/pages/about", "/pages/press"]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Analytics, CDN and boilerplate addresses that look like contacts but aren't.
JUNK = re.compile(
    r"(sentry|wixpress|godaddy|squarespace|shopify|example\.|domain\.|"
    r"\.png|\.jpg|\.jpeg|\.gif|\.webp|\.svg|\.js|\.css|sentry\.io|"
    r"yourdomain|email\.com$|test@|noreply|no-reply|donotreply)", re.I)

# Higher score = better target for cold outreach.
def rank(addr: str) -> int:
    local = addr.split("@", 1)[0].lower()
    if any(k in local for k in ("partnership", "press", "marketing", "media", "wholesale", "brand")):
        return 3
    if any(k in local for k in ("hello", "info", "contact", "team", "hi@", "sales")):
        return 2
    if any(k in local for k in ("help", "support", "care", "service", "orders", "cs@")):
        return 1
    return 2  # a personal-looking address (first@brand) — treat as mid-high


def fetch(url: str, timeout: int = 9) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # some DTC certs chain oddly; we read public HTML only
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(900_000)
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def emails_for(domain: str, verbose=False):
    """Return (best, all_found, source_path) for one domain."""
    domain = domain.strip().rstrip("/")
    if not domain:
        return None, [], ""
    if not domain.startswith("http"):
        base = "https://" + domain.replace("https://", "").replace("http://", "")
    else:
        base = domain
    found = {}
    for path in PATHS:
        url = base + path
        try:
            html = fetch(url)
        except Exception as e:
            if verbose:
                print(f"      {path or '/'} -> {type(e).__name__}")
            continue
        hits = set()
        for m in re.findall(r'mailto:([^"\'?>\s]+)', html, re.I):
            hits.add(m)
        for m in EMAIL_RE.findall(html):
            hits.add(m)
        for h in hits:
            h = h.strip().strip(".,;:").lower()
            if JUNK.search(h) or len(h) > 60:
                continue
            # keep only addresses on the brand's own domain — a Shopify app's
            # address in the page source is not a contact for this brand
            root = base.split("//")[-1].replace("www.", "").split("/")[0]
            core = root.split(".")[0]
            if core and core not in h:
                continue
            found.setdefault(h, path or "/")
        if found:
            break  # homepage/earliest path won; don't hammer the rest
        time.sleep(0.4)
    if not found:
        return None, [], ""
    best = sorted(found, key=lambda a: (-rank(a), len(a)))[0]
    return best, sorted(found, key=lambda a: (-rank(a), len(a))), found[best]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default=None)
    ap.add_argument("--wave", default=None)
    ap.add_argument("--brand", default=None)
    ap.add_argument("--write", action="store_true", help="write results back to the tracker")
    ap.add_argument("--refresh", action="store_true", help="re-look-up rows that already have an email")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.2, help="seconds between domains")
    a = ap.parse_args()

    with open(TRACKER, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
        fields = list(rd.fieldnames)
    for col in ("email_generic", "email_generic_source", "email_alternates"):
        if col not in fields:
            fields.append(col)

    todo = []
    for r in rows:
        r.setdefault("email_generic", ""); r.setdefault("email_generic_source", ""); r.setdefault("email_alternates", "")
        if a.brand and r["brand"].lower() != a.brand.lower():
            continue
        if a.status and (r.get("status") or "").strip() != a.status:
            continue
        if a.wave and (r.get("wave") or "").strip() != str(a.wave):
            continue
        if (r["email_generic"] or "").strip() and not a.refresh:
            continue
        todo.append(r)
    if a.limit:
        todo = todo[:a.limit]

    print(f"{len(todo)} brand(s) to look up{'' if a.write else '  [DRY RUN — pass --write to save]'}\n")
    hits = 0
    for i, r in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {r['brand']:<24} {r['domain']}")
        try:
            best, allf, src = emails_for(r["domain"])
        except Exception as e:
            print(f"      ERROR {type(e).__name__}: {e}")
            best, allf, src = None, [], ""
        if best:
            hits += 1
            tier = {3: "press/partnerships", 2: "general", 1: "support desk"}[rank(best)]
            print(f"      ✓ {best}   ({tier})")
            if len(allf) > 1:
                print(f"        also: {', '.join(allf[1:5])}")
            r["email_generic"] = best
            r["email_generic_source"] = f"{r['domain']}{src}, auto {time.strftime('%Y-%m-%d')}"
            r["email_alternates"] = "; ".join(allf[1:6])
        else:
            print("      ✗ nothing published")
            r["email_generic_source"] = f"no published address found, auto {time.strftime('%Y-%m-%d')}"
        time.sleep(a.delay)

    print(f"\nfound {hits}/{len(todo)}")
    if a.write:
        shutil.copy(TRACKER, TRACKER + ".bak-pre-email-lookup")
        with open(TRACKER, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(rows)
        print(f"tracker written (backup: {os.path.basename(TRACKER)}.bak-pre-email-lookup)")
    else:
        print("dry run — nothing written")


if __name__ == "__main__":
    main()
