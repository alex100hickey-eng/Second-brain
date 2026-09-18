#!/usr/bin/env python3
"""Find a real product photo on a brand's own site, and say which product it is.

Why this exists (2026-09-18): the `offer` arm of the close experiment promises to build a
static ad from the brand's OWN photo, and `splitframe_queue.py add --close offer` requires
`--offer-image` for exactly that reason — when a founder says "sure, send it", the photo has
to already be chosen rather than hunted for under a same-day promise.

Two things went wrong without this:

  1. **The arm starved.** Finding a photo by hand is slow, so the drafting worker fell back to
     the `question` close whenever it could not find one. The experiment ran 30 question to 3
     offer — and a starved arm teaches nothing, which defeats the point of running it.
  2. **The photo was wrong.** Busy Bees' email promised "the pillar candle photo on your site"
     while the stored URL was the shop's opengraph card — a social share banner, not a pillar
     candle. `fabrication_risk` cannot catch this: it checks claims about work done, and this
     is a claim about a photo.

The reliable route, verified by hand first: a Shopify product page's `og:image` IS that
product's hero shot. So find the product page, take its og:image, and report the product's
real title so the email can name it accurately instead of guessing.

    python3 scripts/find_product_image.py --domain saltair.com
    python3 scripts/find_product_image.py --domain antlerfarms.com --match "deer antler"
    python3 scripts/find_product_image.py --domain shop.busybeescandleco.com --match pillar

Read-only. Public pages on the brand's own domain, nothing else.
"""
import argparse
import html as html_mod
import re
import ssl
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")

# Where product links live. /collections/all is the Shopify convention and carries the whole
# catalogue; the others are fallbacks for stores that renamed or never built it.
LISTING_PATHS = ["/collections/all", "/collections", "/", "/shop", "/products"]

# Images that are on the brand's domain but are not a product: share cards, logos, payment
# icons, badges. Sending one of these as "your product photo" is the failure this file exists
# to stop, so the filter is deliberately broad.
NOT_A_PRODUCT = re.compile(
    r"(opengraph|og[-_]image|social[-_]share|share[-_]card|logo|favicon|icon|badge|"
    r"placeholder|swatch|payment|visa|mastercard|paypal|amex|klarna|afterpay|"
    r"sprite|banner|hero[-_]bg|background|pattern|texture[-_]bg|loading|spinner)", re.I)

# Slugs that are listed under /products/ but are not a product you can photograph for an ad.
# A gift card ranked first for Gracie's Doggie Delights and would have been sent as "your
# product shot" — the exact failure this file exists to prevent, produced by this file.
NOT_A_PRODUCT_SLUG = re.compile(
    r"(gift[-_]?card|e[-_]?gift|giftcard|shipping[-_]protection|route[-_]insurance|"
    r"warranty|donation|tip|gratuity|subscription[-_]only|test[-_]product|"
    r"hidden[-_]product|product[-_]customizer)", re.I)

OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)
OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)
PRODUCT_HREF = re.compile(r'href=["\']([^"\']*?/products/[^"\'?#]+)["\']', re.I)


def fetch(url: str, timeout: int = 10) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # public HTML only; some DTC certs chain oddly
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(900_000)
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def base_url(domain: str) -> str:
    d = (domain or "").strip().rstrip("/")
    if not d:
        return ""
    return d if d.startswith("http") else "https://" + d.replace("https://", "").replace("http://", "")


def normalise(url: str, base: str) -> str:
    url = html_mod.unescape((url or "").strip())
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return url


def product_links(base: str, match: str = "", limit: int = 12) -> list:
    """Product page paths on this domain, best matches first."""
    found, seen = [], set()
    for path in LISTING_PATHS:
        try:
            html = fetch(base + path)
        except Exception:                                  # noqa: BLE001
            continue
        for href in PRODUCT_HREF.findall(html):
            href = html_mod.unescape(href)
            if href.startswith("http"):
                root = base.split("//")[-1].split("/")[0].lower()
                if root not in href.lower():
                    continue                               # someone else's store
                href = "/" + href.split("//")[-1].split("/", 1)[-1]
            slug = href.split("?")[0].split("#")[0].rstrip("/")
            if not slug or slug in seen or NOT_A_PRODUCT_SLUG.search(slug):
                continue
            seen.add(slug)
            found.append(slug)
        if found:
            break
        time.sleep(0.3)
    if match:
        wanted = [w for w in re.split(r"[^a-z0-9]+", match.lower()) if w]

        # EVERY meaningful word has to be present, not just one. "body serum" matched
        # "baby-serum" on the word "serum" alone and would have been sent as the brand's own
        # body serum shot — a wrong claim, made out loud, in an email promising that exact
        # product. Short words are dropped so "3 x 6 pillar" is not defeated by "x".
        need = [w for w in wanted if len(w) >= 3]

        def all_present(slug):
            s = slug.lower()
            return need and all(w in s for w in need)
        matched = [s for s in found if all_present(s)]
        if matched:
            matched.sort(key=len)          # the tightest slug is the most specific product
            return matched[:limit]
        # Nothing matched every word. Say so rather than guessing: the caller asked for a
        # specific product because the email names one.
        return []
    return found[:limit]


def image_for_product(url: str, base: str):
    """(image url, product title) from one product page, or (None, '')."""
    try:
        html = fetch(url)
    except Exception:                                      # noqa: BLE001
        return None, ""
    m = OG_IMAGE.search(html) or OG_IMAGE_ALT.search(html)
    if not m:
        return None, ""
    img = normalise(m.group(1), base)
    if NOT_A_PRODUCT.search(img):
        # A store whose product page shares the site-wide social card. That card is not this
        # product, so it is not usable for a promise that names the product.
        return None, ""
    if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", img, re.I):
        return None, ""
    t = OG_TITLE.search(html)
    title = html_mod.unescape(t.group(1)).strip() if t else ""
    return img, title


def find(domain: str, match: str = "", tries: int = 6, verbose: bool = False):
    """(image url, product title, product page) — the first product page that yields a real photo."""
    base = base_url(domain)
    if not base:
        return None, "", ""
    links = product_links(base, match)
    if verbose:
        print(f"  {len(links)} product link(s); trying {min(tries, len(links))}")
    for slug in links[:tries]:
        page = base + slug
        img, title = image_for_product(page, base)
        if verbose:
            print(f"    {slug} -> {'ok' if img else 'no usable og:image'}")
        if img:
            return img, title, page
        time.sleep(0.3)
    return None, "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--match", default="", help="words from the product the email names")
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    img, title, page = find(a.domain, a.match, a.tries, a.verbose)
    if not img:
        print(f"no usable product photo found on {a.domain}"
              + (f" matching {a.match!r} — try different words, or the email should name a "
                 "product they actually sell" if a.match else ""))
        return 1
    print(f"image:   {img}")
    print(f"product: {title or '(untitled)'}")
    print(f"page:    {page}")
    print("\nName THIS product in the email — the promise has to match the photo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
