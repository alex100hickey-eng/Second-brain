#!/usr/bin/env python3
"""spec_ad.py — render a real, uploadable static ad from a brand's own product photo.

Why this exists: the outreach machine can now offer to MAKE the test it just described,
free, and that arm of the experiment is only honest if the thing can actually be made.
Everything upstream of here produces text — angles, concepts, scripts. A founder who says
"sure, send it" is expecting a file they can put in Ads Manager, not a document.

So this renders one. HTML and CSS through headless Chrome, which is already on the Mac:
no credits, no subscription, no third-party render service, and real typography.

Two rules it enforces rather than trusts:

  1. THE PRODUCT PHOTO IS THEIRS. --image takes a URL on the brand's own site (or a file
     already downloaded from it). Nothing is generated, substituted or stock. A spec ad
     showing a product the brand does not sell is worse than sending nothing.
  2. NO CLAIM THAT WAS NOT GIVEN TO IT. The same fabrication guard the cold emails use
     runs over every line of copy, plus the claims a static ad invents most easily —
     percentages, "#1", "clinically proven", invented review counts. A spec ad is
     unsolicited, goes out under Alex's name, and is the first work a brand sees.

Usage:
    python3 scripts/spec_ad.py render --brand "Calypsa" \\
        --headline "Swim you can actually swim in" \\
        --body "UPF 50+. Full coverage. No rash guard needed." \\
        --cta "Shop the collection" \\
        --image https://calypsa.com/cdn/shop/files/suit.jpg \\
        --out ~/Desktop/calypsa-spec.png

    python3 scripts/spec_ad.py render ... --ratio 1:1 --theme dark --dry-run

    python3 scripts/spec_ad.py render ... --layout feed        # or: card (default), editorial

Layouts (--layout):
    card       photo on top, flat panel below, pill button. The original; its output never
               changes, so every render already approved stays reproducible.
    feed       paid-social static: photo full-bleed, dark scrim over the bottom 45%, big bold
               headline in white, brand wordmark top-left, white pill CTA bottom-left.
    editorial  photo over the top 62%, big serif headline on a panel coloured from the photo
               itself, body in small caps, CTA as underlined text.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reuse the email guard instead of writing a second one: a copied guard drifts, and this one
# decides whether a stranger receives a claim about their own product that nobody checked.
_SPEC = importlib.util.spec_from_file_location(
    "splitframe_daily", os.path.join(ROOT, "scripts", "splitframe_daily.py"))
_sfd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_sfd)
fabrication_risk = _sfd.fabrication_risk

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")

# 4:5 is the default because it is the tallest ratio Meta serves in-feed without cropping,
# so it buys the most screen for the same spend. 1:1 is there for placements that need it.
RATIOS = {"4:5": (1080, 1350), "1:1": (1080, 1080)}

RENDER_TIMEOUT_S = 45.0

HEADLINE_MAX = 65          # past this the feed truncates it and the ad argues with itself
BODY_MAX = 140

# What a static ad invents most easily. Each of these is a claim someone has to be able to
# back, and on a spec ad for a brand nobody has spoken to yet, nobody can.
INVENTED = [
    (re.compile(r"\d+\s?%"), "a percentage"),
    (re.compile(r"(?<!\w)#\s?1\b|\bnumber one\b", re.I), '"#1"'),
    (re.compile(r"\bclinically\b|\bdermatologist[- ]tested\b|\bFDA\b", re.I), "a medical claim"),
    (re.compile(r"\b\d[\d,]{2,}\s*(reviews?|customers?|five[- ]star)", re.I), "a review count"),
    (re.compile(r"\b(guaranteed|proven|best)\b", re.I), "an absolute claim"),
    (re.compile(r"\$\s?\d"), "a price"),
]


def copy_problems(headline: str, body: str, cta: str, allow: list) -> list:
    """Every reason this copy must not go out on a spec ad."""
    problems = []
    h, b = (headline or "").strip(), (body or "").strip()
    if not h:
        problems.append("no headline")
    if len(h) > HEADLINE_MAX:
        problems.append(f"headline is {len(h)} chars; past {HEADLINE_MAX} the feed truncates it")
    if len(b) > BODY_MAX:
        problems.append(f"body is {len(b)} chars; past {BODY_MAX} nobody reads it on a phone")
    risky = fabrication_risk(f"{h} {b} {cta}")
    if risky:
        problems.append("claims work not done: " + ", ".join(risky))
    for pattern, what in INVENTED:
        if what in allow:
            continue
        if pattern.search(f"{h} {b} {cta}"):
            problems.append(f"{what} — a spec ad may only carry a claim the brand itself "
                            f"published; pass --allow {what!r} once you have read it on their site")
    return problems


def fetch_image(src: str, dest_dir: str) -> tuple:
    """(local path, problem). Only the brand's own photo ever reaches the canvas."""
    if not src:
        return "", "no --image: a spec ad without the real product is not a spec ad"
    if not src.lower().startswith(("http://", "https://")):
        path = os.path.expanduser(src)
        return (path, "") if os.path.exists(path) else ("", f"{src} does not exist")
    try:
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
    except Exception as e:                                   # noqa: BLE001
        return "", f"could not download the product photo: {str(e)[:120]}"
    if not ctype.startswith("image/") and not src.lower().split("?")[0].endswith(
            (".jpg", ".jpeg", ".png", ".webp")):
        return "", f"that URL is {ctype or 'not an image'}, not a product photo"
    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    path = os.path.join(dest_dir, "product" + ext)
    with open(path, "wb") as f:
        f.write(data)
    return path, ""


# A product photo is usually taller than the frame it lands in, and "center" is the one crop
# that reliably cuts a model's head off — it did exactly that on the first ad this rendered.
# Top is the safe default for anything with a person in it.
FOCUS = {"top": "center top", "center": "center center", "bottom": "center bottom"}

THEMES = {
    "light": {"bg": "#F4F1EC", "ink": "#14110E", "muted": "#5C564E", "chip": "#14110E",
              "chip_ink": "#F4F1EC"},
    "dark": {"bg": "#14110E", "ink": "#F7F4EF", "muted": "#A49C90", "chip": "#F7F4EF",
             "chip_ink": "#14110E"},
}


LAYOUTS = ("card", "feed", "editorial")

# The theme a layout gets when --theme is not passed. card keeps the light default it always had.
# feed is white type on a dark scrim by definition; "auto" lets editorial take its panel from the
# photo and pick whichever ink reads best on it.
LAYOUT_THEME = {"card": "light", "feed": "dark", "editorial": "auto"}

# System faces only: the render is offline, so a web font would silently fall back to whatever
# Chrome picks. Every family here ships with macOS; the tail of each stack is the safety net.
DISPLAY_FONT = '"Avenir Next", "Helvetica Neue", -apple-system, Helvetica, Arial, sans-serif'
SERIF_FONT = '"Iowan Old Style", "New York", Georgia, "Times New Roman", serif'

INK_DARK = (0x14, 0x11, 0x0E)
INK_LIGHT = (0xF7, 0xF4, 0xEF)


def _hex(rgb: tuple) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def _luminance(rgb: tuple) -> float:
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple, b: tuple) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _open_rgb(image_path: str):
    """The photo as Pillow RGB, transparency flattened onto white (what Chrome shows over the
    white the new layouts put behind the photo). None when Pillow or the file can't do it —
    every caller has a fallback, so a missing Pillow costs polish, never a render."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as im:
            im.draft("RGB", (400, 400))                    # JPEG: decode small, much faster
            im = im.convert("RGBA")
            flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
            flat.alpha_composite(im)
            return flat.convert("RGB")
    except Exception:                                        # noqa: BLE001
        return None


def dominant_colour(image_path: str):
    """The colour a viewer would name for this photo, or None.

    Most frequent is not enough: a studio shot is 70% white seamless, and a panel the colour of
    the backdrop reads as an empty box. So each candidate is weighted by how saturated it is, and
    the near-white / near-black / grey backdrop colours only win when there is nothing else.
    """
    im = _open_rgb(image_path)
    if im is None:
        return None
    from PIL import Image
    im.thumbnail((96, 96))
    q = im.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts = q.getcolors() or []
    total = sum(c for c, _ in counts) or 1
    best, best_score = None, -1.0
    for count, idx in counts:
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        hi, lo = max(rgb), min(rgb)
        sat = (hi - lo) / hi if hi else 0.0
        share = count / total
        backdrop = sat < 0.12 or _luminance(rgb) > 0.86
        score = share * (0.15 + sat) * (0.25 if backdrop else 1.0)
        if share >= 0.04 and score > best_score:
            best, best_score = rgb, score
    return best


def _focus_fraction(focus_css: str) -> tuple:
    """"center top" -> (0.5, 0.0): where background-position anchors the crop."""
    x, y = 0.5, 0.5
    for word in focus_css.split():
        if word in ("left", "right"):
            x = 0.0 if word == "left" else 1.0
        elif word in ("top", "bottom"):
            y = 0.0 if word == "top" else 1.0
    return x, y


def _region_luminance(image_path: str, size: tuple, focus_css: str, box: tuple):
    """Mean luminance of what background-size: cover puts under `box` (x0, y0, x1, y1 in
    frame pixels), or None. Used to pick the wordmark's ink against the photo it sits on."""
    im = _open_rgb(image_path)
    if im is None:
        return None
    w, h = size
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    fx, fy = _focus_fraction(focus_css)
    ox, oy = (w - iw * scale) * fx, (h - ih * scale) * fy
    x0, y0, x1, y1 = box
    crop = im.crop((int(max(0, (x0 - ox) / scale)), int(max(0, (y0 - oy) / scale)),
                    int(min(iw, (x1 - ox) / scale)), int(min(ih, (y1 - oy) / scale))))
    if crop.width < 1 or crop.height < 1:
        return None
    crop.thumbnail((64, 64))
    raw = crop.tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    return sum(_luminance(p) for p in px) / len(px)


def editorial_palette(dominant, theme: str) -> tuple:
    """(panel, ink) for the editorial panel.

    The panel starts as the photo's own colour and is pushed toward the paper (light theme) or
    the ink (dark theme) only as far as it takes for the small-caps body to read at 6:1. auto
    keeps whichever ink already reads better, so a mint bottle gets a mint panel with dark type
    and a dark bottle a dark panel with light type. No Pillow, or a photo it cannot open: the
    near-black the card's dark theme uses.
    """
    base = tuple(dominant) if dominant else INK_DARK
    if theme == "light":
        ink = INK_DARK
    elif theme == "dark":
        ink = INK_LIGHT
    else:
        ink = INK_LIGHT if _contrast(base, INK_LIGHT) >= _contrast(base, INK_DARK) else INK_DARK
    away = INK_LIGHT if ink == INK_DARK else INK_DARK
    panel = base
    for step in range(21):
        panel = _mix(base, away, step / 20)
        if _contrast(panel, ink) >= 6.0:
            break
    return panel, ink


def _image_uri(image_path: str) -> str:
    # The photo is inlined as a data URI rather than referenced as file://. A file:// page
    # loading a file:// image is a cross-origin read that headless Chrome will sit on rather
    # than refuse — the screenshot never lands and the render just times out.
    import base64
    import mimetypes
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode("ascii")


# Fit scripts. They run inside the page, where the real font metrics are, and run again once
# fonts settle: a long headline is set smaller instead of clipped. They only ever shrink from
# the start size. The card layout carries none: its output is frozen.
_FEED_FIT_JS = """<script>
(function () {
  function px(el) { return parseFloat(getComputedStyle(el).fontSize); }
  function fit() {
    var box = document.getElementById("fitbox"), h = document.getElementById("headline");
    var p = document.getElementById("body"), scrim = document.getElementById("scrim");
    var size = px(h);
    // Two lines while the type stays display-sized: a third line eats the photo.
    while (h.offsetHeight > size * 2.2 && size > %(two_line_min)d) { size -= 2; h.style.fontSize = size + "px"; }
    while (box.offsetHeight > %(max_copy)d && size > %(head_min)d) { size -= 2; h.style.fontSize = size + "px"; }
    var bs = p ? px(p) : 0;
    while (p && box.offsetHeight > %(max_copy)d && bs > 22) { bs -= 1; p.style.fontSize = bs + "px"; }
    // At least the bottom 45%%, and always a full ramp above the first line of type, so the
    // headline starts on shade rather than on bare photo.
    scrim.style.height = Math.max(%(scrim)d, box.offsetHeight + %(pad)d + %(ramp)d) + "px";
  }
  fit();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();
</script>"""

_EDITORIAL_FIT_JS = """<script>
(function () {
  function px(el) { return parseFloat(getComputedStyle(el).fontSize); }
  function fit() {
    var box = document.getElementById("fitbox"), h = document.getElementById("headline");
    var p = document.getElementById("body");
    function over() { return box.scrollHeight > box.clientHeight + 1; }
    var size = px(h);
    while (over() && size > 40) { size -= 2; h.style.fontSize = size + "px"; }
    var bs = p ? px(p) : 0;
    while (p && over() && bs > 20) { bs -= 1; p.style.fontSize = bs + "px"; }
  }
  fit();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();
</script>"""


def _headline_px(headline: str, sizes: tuple) -> int:
    """Start size by length; the fit script only ever shrinks from here."""
    n = len(headline)
    return sizes[0] if n <= 24 else sizes[1] if n <= 40 else sizes[2]


def _feed_html(brand, headline, body, cta, image_path, theme, size, focus) -> str:
    """Paid-social static: the photo is the ad, the copy sits in it.

    What a brand's own best static usually is — full-bleed product, a scrim, one loud line —
    rather than a product card. The scrim covers the bottom 45% and grows only if the copy
    needs more room, so the type never sits on bare photo. --theme light swaps the dark scrim
    and white type for the card's paper and ink.
    """
    esc = html.escape
    w, h = size
    pad = 64
    if theme == "light":
        scrim_rgb, ink, pill_bg, pill_ink = (0xF4, 0xF1, 0xEC), _hex(INK_DARK), _hex(INK_DARK), "#F4F1EC"
        shadow = "none"
    else:
        scrim_rgb, ink, pill_bg, pill_ink = (0x0B, 0x09, 0x08), "#FFFFFF", "#FFFFFF", _hex(INK_DARK)
        shadow = "0 2px 18px rgba(0,0,0,.28)"
    r, g, b = scrim_rgb
    # The ramp is in pixels from the scrim's top edge, so it stays the same soft fade when the
    # fit script grows the scrim for a long headline. Half-dark by the end of the ramp is
    # where white type starts to clear 4.5:1 even over a white backdrop.
    ramp = 200 if h >= 1300 else 170
    stops = ", ".join(f"rgba({r},{g},{b},{a}) {pos}" for a, pos in
                      ((0, "0px"), (.1, f"{ramp * .3:.0f}px"), (.27, f"{ramp * .6:.0f}px"),
                       (.5, f"{ramp}px"), (.84, "100%")))
    # The wordmark sits on bare photo: dark ink on a pale corner, white on anything else.
    corner = _region_luminance(image_path, size, focus, (pad, 40, pad + 440, 40 + 90))
    mark_dark = corner is not None and corner > 0.55
    mark_ink = _hex(INK_DARK) if mark_dark else "#FFFFFF"
    mark_shadow = "none" if mark_dark else "0 1px 12px rgba(0,0,0,.35)"
    head_px = _headline_px(headline, (112, 96, 82))
    body_px = 34 if len(body) <= 60 else 30
    scrim_h = round(h * 0.45)
    fit = _FEED_FIT_JS % dict(two_line_min=round(head_px * 0.72), head_min=48,
                              max_copy=round(h * 0.40), scrim=scrim_h, pad=pad, ramp=ramp)
    return f"""<!doctype html>
<meta charset="utf-8">
<style>
  @page {{ margin: 0 }}
  * {{ box-sizing: border-box; margin: 0; padding: 0 }}
  html, body {{ width: {w}px; height: {h}px; }}
  body {{
    position: relative; overflow: hidden; background: #FFFFFF;
    font-family: {DISPLAY_FONT};
    -webkit-font-smoothing: antialiased;
  }}
  .shot {{
    position: absolute; inset: 0; background-color: #FFFFFF;
    background-image: url("{_image_uri(image_path)}"); background-repeat: no-repeat;
    background-size: cover; background-position: {focus};
  }}
  .scrim {{
    position: absolute; left: 0; right: 0; bottom: 0; height: {scrim_h}px;
    background: linear-gradient(to bottom, {stops});
  }}
  .wordmark {{
    position: absolute; top: 52px; left: {pad}px; max-width: 60%;
    font-size: 30px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase;
    color: {mark_ink}; text-shadow: {mark_shadow}; white-space: nowrap; overflow: hidden;
  }}
  .copy {{ position: absolute; left: {pad}px; right: {pad}px; bottom: {pad}px; color: {ink}; }}
  h1 {{
    font-size: {head_px}px; line-height: .98; letter-spacing: -.028em; font-weight: 800;
    text-wrap: balance; text-shadow: {shadow};
  }}
  p {{
    margin-top: 22px; font-size: {body_px}px; line-height: 1.26; font-weight: 500;
    text-shadow: {shadow}; text-wrap: balance;
  }}
  .cta {{
    display: inline-block; margin-top: 34px; padding: 22px 42px; border-radius: 999px;
    background: {pill_bg}; color: {pill_ink}; font-size: 29px; font-weight: 700;
    letter-spacing: -.005em;
  }}
</style>
<body class="layout-feed">
<div class="shot"></div>
<div class="scrim" id="scrim"></div>
<div class="wordmark">{esc(brand)}</div>
<div class="copy" id="fitbox">
  <h1 id="headline">{esc(headline)}</h1>
  {f'<p id="body">{esc(body)}</p>' if body else ""}
  {f'<div class="cta">{esc(cta)}</div>' if cta else ""}
</div>
{fit}
</body>
"""


def _editorial_html(brand, headline, body, cta, image_path, theme, size, focus) -> str:
    """Magazine page: photo above, a serif headline on a panel coloured from the photo.

    The panel colour comes from the product, so no two brands get the same ad, and the page
    reads as art direction rather than a template. Body in small caps, CTA as an underlined
    line rather than a button: an editorial page does not shout.
    """
    esc = html.escape
    w, h = size
    panel, ink = editorial_palette(dominant_colour(image_path), theme)
    head_px = _headline_px(headline, (108, 96, 84))
    body_px = 28 if len(body) <= 70 else 25
    fit = _EDITORIAL_FIT_JS
    return f"""<!doctype html>
<meta charset="utf-8">
<style>
  @page {{ margin: 0 }}
  * {{ box-sizing: border-box; margin: 0; padding: 0 }}
  html, body {{ width: {w}px; height: {h}px; }}
  body {{
    display: flex; flex-direction: column; overflow: hidden;
    background: {_hex(panel)}; color: {_hex(ink)};
    font-family: {SERIF_FONT};
    -webkit-font-smoothing: antialiased;
  }}
  .shot {{
    flex: 0 0 62%; background-color: #FFFFFF;
    background-image: url("{_image_uri(image_path)}"); background-repeat: no-repeat;
    background-size: cover; background-position: {focus};
  }}
  .panel {{
    flex: 1 1 auto; min-height: 0; overflow: hidden;
    display: flex; flex-direction: column; padding: 50px 72px 54px;
  }}
  h1 {{
    font-size: {head_px}px; line-height: 1.02; letter-spacing: -.014em; font-weight: 700;
    text-wrap: balance;
  }}
  p {{
    margin-top: 20px; font-size: {body_px}px; line-height: 1.32;
    font-variant-caps: all-small-caps; letter-spacing: .075em; opacity: .9;
    text-wrap: balance;
  }}
  .foot {{
    margin-top: auto; padding-top: 22px;
    display: flex; justify-content: space-between; align-items: baseline; gap: 32px;
  }}
  .cta {{
    font-size: 30px; font-style: italic;
    text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 9px;
  }}
  .brand {{
    font-size: 21px; letter-spacing: .22em; text-transform: uppercase; opacity: .78;
    white-space: nowrap;
  }}
</style>
<body class="layout-editorial">
<div class="shot"></div>
<div class="panel" id="fitbox">
  <h1 id="headline">{esc(headline)}</h1>
  {f'<p id="body">{esc(body)}</p>' if body else ""}
  <div class="foot">
    <div class="cta">{esc(cta)}</div>
    <div class="brand">{esc(brand)}</div>
  </div>
</div>
{fit}
</body>
"""


def build_html(brand: str, headline: str, body: str, cta: str, image_path: str,
               theme, size: tuple, focus: str = "center top", layout: str = "card") -> str:
    """The page Chrome screenshots, in one of LAYOUTS (theme None = that layout's default).

    card: photo on top, copy underneath on a flat field. Deliberately not a collage. The one
    thing a small brand's ads almost never do is give the product a clean frame and say one
    thing about it — that is the whole point of the test this ad exists to run, so the layout
    has to be the argument, not decoration on top of one. Its output is pinned byte for byte
    in the tests: approved renders must stay reproducible.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; use one of {', '.join(LAYOUTS)}")
    theme = theme or LAYOUT_THEME[layout]
    if layout == "feed":
        return _feed_html(brand, headline, body, cta, image_path, theme, size,
                          FOCUS.get(focus, focus))
    if layout == "editorial":
        return _editorial_html(brand, headline, body, cta, image_path, theme, size,
                               FOCUS.get(focus, focus))
    t = THEMES[theme]
    w, h = size
    focus = FOCUS.get(focus, focus)
    esc = html.escape
    img_uri = _image_uri(image_path)
    return f"""<!doctype html>
<meta charset="utf-8">
<style>
  @page {{ margin: 0 }}
  * {{ box-sizing: border-box; margin: 0; padding: 0 }}
  html, body {{ width: {w}px; height: {h}px; }}
  body {{
    background: {t['bg']}; color: {t['ink']};
    font-family: "Helvetica Neue", -apple-system, Helvetica, Arial, sans-serif;
    display: flex; flex-direction: column;
  }}
  .shot {{
    flex: 1 1 auto; min-height: 0;
    background-image: url("{img_uri}");
    background-size: cover; background-position: {focus};
  }}
  .copy {{ flex: 0 0 auto; padding: 64px 72px 72px; }}
  .brand {{
    font-size: 26px; letter-spacing: .22em; text-transform: uppercase;
    color: {t['muted']}; margin-bottom: 26px;
  }}
  h1 {{
    font-size: {74 if len(headline) < 34 else 60}px; line-height: 1.06;
    letter-spacing: -.022em; font-weight: 700; text-wrap: balance;
  }}
  p {{ margin-top: 24px; font-size: 32px; line-height: 1.38; color: {t['muted']}; }}
  .cta {{
    display: inline-block; margin-top: 40px; padding: 20px 38px; border-radius: 999px;
    background: {t['chip']}; color: {t['chip_ink']}; font-size: 28px; font-weight: 600;
  }}
</style>
<div class="shot"></div>
<div class="copy">
  <div class="brand">{esc(brand)}</div>
  <h1>{esc(headline)}</h1>
  {f"<p>{esc(body)}</p>" if body else ""}
  {f'<div class="cta">{esc(cta)}</div>' if cta else ""}
</div>
"""


def render(html_text: str, out_path: str, size: tuple) -> str:
    """Headless Chrome writes the PNG itself, so the result is a file on disk rather than a
    screenshot that only exists in a transcript. A throwaway profile, never the operator's —
    a worker may be holding that one, and Chrome refuses a profile that is already open."""
    if not os.path.exists(CHROME):
        return "Google Chrome is not installed at the expected path"
    w, h = size
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "ad.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html_text)
        out_path = os.path.expanduser(out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        # A leftover file from an earlier render reads as "stable" before Chrome has written
        # anything, so a re-render would stop early and keep the stale ad.
        if os.path.exists(out_path):
            os.remove(out_path)
        # Chrome writes the screenshot and then does not exit — waiting on the process hangs
        # for as long as you let it, on a render that already finished. So watch for the file
        # instead of the exit code, and stop the process once the bytes have settled.
        proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--virtual-time-budget=5000",
             "--no-sandbox", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions",
             f"--user-data-dir={os.path.join(tmp, 'profile')}",
             f"--window-size={w},{h}", f"--screenshot={out_path}",
             "file://" + page],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            size, stable, waited = -1, 0, 0.0
            while waited < RENDER_TIMEOUT_S:
                now = os.path.getsize(out_path) if os.path.exists(out_path) else -1
                stable = stable + 1 if now == size and now > 0 else 0
                size = now
                if stable >= 2:                   # two identical reads: the file is finished
                    break
                time.sleep(0.25)
                waited += 0.25
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return f"Chrome wrote nothing in {RENDER_TIMEOUT_S:.0f}s"
    return ""


def elide_photo(page: str) -> str:
    """The page as --dry-run prints it: the inlined photo cut to a stub, so a check prints a
    few KB of HTML instead of a megabyte of base64 into a terminal or a worker's context."""
    return re.sub(r"(data:[\w/+.-]+;base64,)([A-Za-z0-9+/=]{2000,})",
                  lambda m: f"{m.group(1)}...[{len(m.group(2)) * 3 // 4 // 1024} KB photo elided]",
                  page)


def cmd_render(args) -> int:
    problems = copy_problems(args.headline, args.body, args.cta, args.allow or [])
    if args.ratio not in RATIOS:
        problems.append(f"--ratio must be one of {', '.join(RATIOS)}")
    if problems:
        print("NOT rendered:")
        for p in problems:
            print(f"  - {p}")
        return 1
    tmp = tempfile.mkdtemp(prefix="spec-ad-")
    try:
        image, problem = fetch_image(args.image, tmp)
        if problem:
            print(f"NOT rendered:\n  - {problem}")
            return 1
        size = RATIOS[args.ratio]
        layout = getattr(args, "layout", "card")
        page = build_html(args.brand, args.headline, args.body, args.cta, image,
                          args.theme, size, args.focus, layout)
        shape = args.ratio if layout == "card" else f"{args.ratio} {layout}"
        if args.dry_run:
            print(f"OK (dry run): {args.brand} {shape} passes every guard; "
                  f"product photo {os.path.basename(image)}; nothing written.")
            print(elide_photo(page))
            return 0
        err = render(page, args.out, size)
        if err:
            print(f"NOT rendered:\n  - {err}")
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = os.path.expanduser(args.out)
    print(f"RENDERED: {out} ({shape}, {os.path.getsize(out) // 1024} KB)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render one static ad to a PNG")
    r.add_argument("--brand", required=True)
    r.add_argument("--headline", required=True)
    r.add_argument("--body", default="")
    r.add_argument("--cta", default="")
    r.add_argument("--image", required=True, help="the brand's OWN product photo (url or path)")
    r.add_argument("--out", required=True)
    r.add_argument("--ratio", default="4:5", choices=list(RATIOS))
    r.add_argument("--layout", default="card", choices=list(LAYOUTS),
                   help="card (default, the original: photo over a flat panel), feed "
                        "(full-bleed photo, dark scrim, big white headline, pill CTA), "
                        "editorial (photo over a serif headline on a panel coloured from "
                        "the photo)")
    r.add_argument("--theme", default=None, choices=list(THEMES),
                   help="default: light for card, dark for feed, sampled from the photo "
                        "for editorial")
    r.add_argument("--focus", default="top", choices=list(FOCUS),
                   help="which part of a too-tall photo survives the crop (default: top)")
    r.add_argument("--allow", action="append", default=[],
                   help="a claim you have READ on their own site, e.g. --allow 'a percentage'")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_render)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
