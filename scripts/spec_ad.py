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


def build_html(brand: str, headline: str, body: str, cta: str, image_path: str,
               theme: str, size: tuple, focus: str = "center top") -> str:
    """The layout: photo on top, copy underneath on a flat field.

    Deliberately not a collage. The one thing a small brand's ads almost never do is give the
    product a clean frame and say one thing about it — that is the whole point of the test this
    ad exists to run, so the layout has to be the argument, not decoration on top of one.
    """
    t = THEMES[theme]
    w, h = size
    focus = FOCUS.get(focus, focus)
    esc = html.escape
    # The photo is inlined as a data URI rather than referenced as file://. A file:// page
    # loading a file:// image is a cross-origin read that headless Chrome will sit on rather
    # than refuse — the screenshot never lands and the render just times out.
    import base64
    import mimetypes
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as fh:
        img_uri = f"data:{mime};base64," + base64.b64encode(fh.read()).decode("ascii")
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
        page = build_html(args.brand, args.headline, args.body, args.cta, image,
                          args.theme, size, args.focus)
        if args.dry_run:
            print(f"OK (dry run): {args.brand} {args.ratio} passes every guard; "
                  f"product photo {os.path.basename(image)}; nothing written.")
            return 0
        err = render(page, args.out, size)
        if err:
            print(f"NOT rendered:\n  - {err}")
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = os.path.expanduser(args.out)
    print(f"RENDERED: {out} ({args.ratio}, {os.path.getsize(out) // 1024} KB)")
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
    r.add_argument("--theme", default="light", choices=list(THEMES))
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
