#!/usr/bin/env python3
r"""adlib_read.py — read Meta Ad Library pages (and any other page) headless, no desktop pane needed.

The money operator's worker is a headless `claude -p` spawned by launchd, so it has no in-app
Browser pane. Playwright drives the INSTALLED Google Chrome (channel "chrome", no download) with a
persistent profile and returns the rendered text; raw `chrome --dump-dom` hangs on a persistent
profile (tried 2026-09-16), so it is only the no-profile fallback when Playwright is missing.

  adlib_read.py --page-id 219964211450043          # one advertiser's active ads
  adlib_read.py --keyword "dog supplements"        # advertisers currently spending on a topic
  adlib_read.py --url https://app.vyro.com/...     # any page, as text
  adlib_read.py --url https://www.tiktok.com/@wildest_moments --find 'video/\d{15,}'   # ids from the HTML

All reads use the operator Chrome profile (~/.money-operator-chrome). Alex logs in ONCE per site
with a headed window on that profile:
  open -na "Google Chrome" --args --user-data-dir="$HOME/.money-operator-chrome" https://app.vyro.com
and every headless read after that carries the cookies. Exit codes: 0 ok · 2 render failed ·
3 login wall · 4 no results rendered · 5 profile in use (a headed window is open on it — skip).
Read-only. Never types, clicks or submits anything.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from html.parser import HTMLParser

CHROME = os.environ.get("OPERATOR_CHROME",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PROFILE = os.path.expanduser(os.environ.get("OPERATOR_CHROME_PROFILE", "~/.money-operator-chrome"))
BASE = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US"
LOGIN_TELLS = ("Log in to continue", "Log into Facebook", "Enter your email to log in",
               "Continue with Google", "Sign in to continue", "Log in or sign up")
IN_USE_TELLS = ("in use", "SingletonLock", "ProcessSingleton", "already running")


class ProfileInUse(RuntimeError):
    pass


class _Text(HTMLParser):
    BLOCK = {"div", "p", "br", "span", "a", "li", "h1", "h2", "h3", "h4", "td", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(raw: str) -> str:
    p = _Text()
    p.feed(raw)
    text = html.unescape("".join(p.out))
    text = re.sub(r"[ \t​\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t​\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def render_text(url: str, settle_ms: int = 20000, timeout_s: int = 75, find_re: str = "") -> str:
    """The page as rendered text, through the operator profile."""
    try:
        from playwright.sync_api import sync_playwright, Error as PWError   # type: ignore
    except ImportError:
        return html_to_text(_render_raw(url, settle_ms, timeout_s))
    os.makedirs(PROFILE, exist_ok=True)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE, channel="chrome", headless=True,
                args=["--disable-gpu", "--mute-audio"], timeout=30000)
            try:
                page = ctx.new_page()
                page.set_default_timeout(timeout_s * 1000)
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector("text=/\\d[\\d,]* results|Sponsored|Log in/", timeout=settle_ms)
                except PWError:
                    page.wait_for_timeout(min(settle_ms, 8000))
                text = _clean_text(page.inner_text("body"))
                if find_re:
                    # ids and links live in the HTML/JSON, not the visible text (TikTok video ids, say)
                    hits, seen = [], set()
                    for m in re.finditer(find_re, page.content()):
                        if m.group(0) not in seen:
                            seen.add(m.group(0))
                            hits.append(m.group(0))
                    text = "FOUND: " + (", ".join(hits[:60]) or "(no match)") + "\n" + text
                return text
            finally:
                ctx.close()
    except PWError as e:
        msg = str(e)
        if any(t in msg for t in IN_USE_TELLS):
            raise ProfileInUse(msg[:300]) from e
        raise RuntimeError(msg[:400]) from e


def _render_raw(url: str, budget_ms: int, timeout_s: int) -> str:
    """No-profile fallback: logged-out reads only."""
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--dump-dom",
           f"--virtual-time-budget={budget_ms}", f"--timeout={budget_ms + 10000}", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if not proc.stdout.strip():
        raise RuntimeError(f"Chrome returned nothing (rc={proc.returncode}): {proc.stderr.strip()[-300:]}")
    return proc.stdout


def parse_ads(text: str, max_ads: int = 25, chars: int = 600) -> dict:
    """The results count plus one compact block per ad, from the page text."""
    m = re.search(r"~?\d[\d,]* results", text)
    count = int(re.sub(r"[^\d]", "", m.group(0))) if m else None
    blocks = text.split("Library ID:")[1:]
    ads = []
    for b in blocks[:max_ads]:
        flat = " ".join(b.split())
        started = re.search(r"Started running on ([A-Za-z]{3} \d{1,2}, \d{4})", flat)
        flat = re.sub(r"Open Dropdown|See ad details|See summary details|Platforms", "", flat)
        ads.append({"id": flat.split(" ")[0], "started": started.group(1) if started else "",
                    "text": " ".join(flat.split())[:chars]})
    return {"count": count, "ad_blocks_seen": len(blocks), "ads": ads}


def parse_keyword(text: str, max_rows: int = 40) -> list:
    """Advertisers on a keyword results page: the name line right before each 'Sponsored'."""
    names = []
    for m in re.finditer(r"\n([^\n]{2,80})\nSponsored\n", text):
        name = re.sub(r"\s+with\s+.*$", "", m.group(1).strip())
        if name and name not in names:
            names.append(name)
        if len(names) >= max_rows:
            break
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--page-id", help="Ad Library page id (view_all_page_id)")
    g.add_argument("--keyword", help="Ad Library keyword search")
    g.add_argument("--url", help="any URL, printed as text")
    ap.add_argument("--max-ads", type=int, default=25)
    ap.add_argument("--max-chars", type=int, default=18000)
    ap.add_argument("--settle-ms", type=int, default=20000, help="how long to wait for results to render")
    ap.add_argument("--find", default="", help="regex to pull out of the page HTML (e.g. 'video/\\d{15,}'), printed first")
    a = ap.parse_args(argv)

    if a.page_id:
        url = f"{BASE}&view_all_page_id={a.page_id}"
    elif a.keyword:
        from urllib.parse import quote_plus
        url = f"{BASE}&q={quote_plus(a.keyword)}&search_type=keyword_unordered"
    else:
        url = a.url
    try:
        text = render_text(url, a.settle_ms, find_re=a.find)
    except ProfileInUse as e:
        print(f"PROFILE IN USE — a headed Chrome window is open on {PROFILE}; skip this read and say so. ({e})")
        return 5
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"RENDER FAILED: {e}")
        return 2
    if any(t in text for t in LOGIN_TELLS) and "results" not in text:
        print(f"LOGIN WALL at {url}\n(log in once: open -na \"Google Chrome\" --args "
              f"--user-data-dir=\"{PROFILE}\" \"{url}\")")
        return 3
    if a.url:
        print(text[:a.max_chars])
        return 0
    parsed = parse_ads(text, a.max_ads)
    if parsed["count"] is None and not parsed["ads"]:
        print("NO RESULTS RENDERED — try a longer --settle-ms, or the page id is wrong. Page text head:")
        print(text[:1500])
        return 4
    print(f"URL: {url}")
    print(f"ACTIVE ADS: {parsed['count'] if parsed['count'] is not None else '?'} "
          f"(blocks on first page: {parsed['ad_blocks_seen']})")
    if a.keyword:
        print("ADVERTISERS (in order seen): " + "; ".join(parse_keyword(text)))
    body = "\n".join(f"- [{ad['started'] or 'start ?'}] {ad['text']}" for ad in parsed["ads"])
    print(body[:a.max_chars])
    return 0


if __name__ == "__main__":
    sys.exit(main())
