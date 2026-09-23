"""Captions + the two posting drivers.

folder  — stage the transformed file into iCloud Drive `ClipBot/ready/<platform>/` with a caption
          .txt beside it. Alex posts from the Files app when the window is right. Default.
opus    — schedule OpusClip's own (untransformed) clip through its social poster. Needs the
          accounts connected inside OpusClip. Weaker on originality, zero phone time.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str, n: int = 48) -> str:
    return _SAFE.sub("-", (text or "").strip()).strip("-")[:n] or "clip"


def build_caption(platform: str, clip: dict, campaign: dict, text_hook: str) -> tuple:
    """(title, body). Title from the clip; body = hook line + the brief's mandatory line + tag + hashtags.

    Campaign rules (config.DEFAULT_RULES) decide whether the clip's own hashtags are allowed and which
    caption line / account tag the brief requires verbatim."""
    rules = config.campaign_rules(campaign)
    title = (clip.get("title") or text_hook or "Clip").strip()[: config.TITLE_LIMIT]
    required = [t if t.startswith("#") else f"#{t}" for t in (campaign.get("hashtags") or "").split() if t]
    clip_tags = clip.get("hashtags") or []
    if isinstance(clip_tags, str):
        clip_tags = clip_tags.split()
    clip_tags = [t if t.startswith("#") else f"#{t}" for t in clip_tags][:5] if rules["extra_tags"] else []
    seen, tags = set(), []
    for t in required + clip_tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    tagline = " ".join(x for x in [(rules["tag"] or "").strip(), " ".join(tags)] if x)
    caption = (rules["captions"] or {}).get(platform) or rules["caption"] or ""
    parts = [(text_hook or "").strip(), caption.strip(), tagline]
    body = "\n\n".join(p for p in parts if p).strip()
    return title, body[: config.CAPTION_LIMITS.get(platform, 2000)]


def post_order(ledger, today: str | None = None) -> list:
    """Staged-but-unposted variants ranked: soonest campaign deadline first, then OpusClip score.
    Each row gets a suggested day (0 = today) from the campaign's per_day quota."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    rows = []
    for v in ledger.variants("staged"):
        clip = ledger.clip(v["clip_id"]) or {}
        src = next((s for s in ledger.sources() if s["id"] == clip.get("source_id")), None)
        camp = ledger.campaign(src["campaign_id"]) if src else None
        rules = config.campaign_rules(camp)
        rows.append({"variant": v["id"], "file": os.path.basename(v["staged_path"] or v["path"] or ""),
                     "platform": v["platform"], "campaign": (camp or {}).get("name", "?"), "ends": rules["ends"] or "9999-12-31",
                     "per_day": int(rules["per_day"] or 3), "score": float(clip.get("score") or 0),
                     "seconds": float(clip.get("duration_s") or 0), "line": v.get("text_hook") or ""})
    # soonest deadline, then score; among equals the sub-45 s clip first (completion rate drives reach)
    rows.sort(key=lambda r: (r["ends"], -r["score"], r["seconds"] > 45, r["variant"]))
    rows = _interleave_lines(rows)
    seen = {}
    for r in rows:
        # per_day is a per-ACCOUNT cadence, and each platform is its own account: Reels and Shorts
        # don't spend TikTok's two a day.
        key = (r["campaign"], r["platform"])
        n = seen.get(key, 0)
        r["day"] = n // max(1, r["per_day"])
        seen[key] = n + 1
        r["days_left"] = None if r["ends"] == "9999-12-31" else (datetime.strptime(r["ends"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    return rows


def _interleave_lines(rows: list) -> list:
    """Keep the ranking but never let two consecutive posts of a campaign carry the same caption line:
    a run of identical openers reads as spam to viewers and to TikTok."""
    out, pending = [], list(rows)
    while pending:
        prev = next((r for r in reversed(out) if r["campaign"] == pending[0]["campaign"]), None)
        pick = next((r for r in pending if r["campaign"] != pending[0]["campaign"] or prev is None or r["line"] != prev["line"]), pending[0])
        pending.remove(pick)
        out.append(pick)
    return out


def format_post_order(rows: list) -> str:
    if not rows:
        return "POST ORDER — nothing staged.\n"
    out = [f"POST ORDER — {len(rows)} staged clips · written {datetime.now().strftime('%Y-%m-%d %H:%M')}",
           "Post in this order, top to bottom. Submit each URL on its campaign's board (Whop for Crazy Taxi:",
           "within 30 minutes of posting, from a linked account), then:",
           "  python3 -m clipbot.runner posted --variant <N> --url <post url> [--account @handle]",
           "  python3 -m clipbot.runner submitted --variant <N>", ""]
    day_names = {0: "TODAY", 1: "TOMORROW"}
    cur = None
    rows = sorted(rows, key=lambda r: (r["day"], r["ends"], r["campaign"]))
    for r in rows:
        key = (r["campaign"], r["day"])
        if key != cur:
            cur = key
            left = "" if r["days_left"] is None else f" · campaign ends in {r['days_left']}d"
            out.append(f"## {r['campaign']} — {day_names.get(r['day'], 'day +' + str(r['day']))}{left}")
        out.append(f"  v{r['variant']:<4} {r['file']:<52} {r['platform']:<7} score {r['score']:>3.0f} · {r['seconds']:>3.0f}s"
                   + (f"  · \"{r['line'][:48]}\"" if r["line"] else ""))
    return "\n".join(out) + "\n"


def write_post_order(ledger, ready_dir: str = config.READY_DIR) -> str:
    os.makedirs(ready_dir, exist_ok=True)
    path = os.path.join(ready_dir, "POST ORDER.txt")
    with open(path, "w") as f:
        f.write(format_post_order(post_order(ledger)))
    return path


WINDOW_START_HOUR_ET = {"tiktok": 19, "shorts": 15, "reels": 11, "facebook": 12}


def next_slot(platform: str, now: datetime | None = None, tz: str = "America/New_York") -> datetime:
    """The next posting-window start for this platform, in Eastern time."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    now = now or datetime.now(z)
    if now.tzinfo is None:
        now = now.replace(tzinfo=z)
    hour = WINDOW_START_HOUR_ET.get(platform, 12)
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot <= now:
        slot = slot + timedelta(days=1)
    return slot


def caption_file_text(platform: str, title: str, body: str, campaign: dict, clip: dict, variant_id: int) -> str:
    return "\n".join([
        f"PLATFORM: {platform}   POST WINDOW (ET): {config.POST_WINDOWS_ET.get(platform, 'any')}   "
        f"NEXT SLOT: {next_slot(platform).strftime('%a %b %-d %-I:%M %p')}",
        f"CAMPAIGN: {campaign.get('name')} ({campaign.get('marketplace') or '?'}) · ${campaign.get('rate_per_1k') or 0:.2f}/1k"
        + (f" · cap ${campaign.get('cap_per_clip'):.0f}/clip" if campaign.get("cap_per_clip") else ""),
        f"VARIANT: {variant_id}   after posting: python3 -m clipbot.runner posted --variant {variant_id} --url <post url>",
        "",
        "TITLE:", title,
        "",
        "CAPTION:", body,
        "",
        f"clip score {clip.get('score') or 0:.0f} · {clip.get('duration_s') or 0:.0f}s · staged {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ])


def stage_folder(variant_path: str, platform: str, title: str, caption_text: str, variant_id: int,
                 ready_dir: str = config.READY_DIR) -> str:
    dest_dir = os.path.join(ready_dir, platform)
    os.makedirs(dest_dir, exist_ok=True)
    base = f"{variant_id:04d}_{slug(title)}"
    dest = os.path.join(dest_dir, base + ".mp4")
    shutil.copy2(variant_path, dest)
    with open(os.path.join(dest_dir, base + ".txt"), "w") as f:
        f.write(caption_text + "\n")
    return dest


def account_for(accounts: list, platform: str) -> dict | None:
    want = {"tiktok": "TIKTOK_BUSINESS", "shorts": "YOUTUBE", "reels": "INSTAGRAM_BUSINESS",
            "facebook": "FACEBOOK_PAGE"}.get(platform)
    return next((a for a in accounts if a.get("platform") == want), None)
