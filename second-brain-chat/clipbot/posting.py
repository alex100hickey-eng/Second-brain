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
from datetime import datetime

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str, n: int = 48) -> str:
    return _SAFE.sub("-", (text or "").strip()).strip("-")[:n] or "clip"


def build_caption(platform: str, clip: dict, campaign: dict, text_hook: str) -> tuple:
    """(title, body). Title from the clip; body = hook line + campaign's required tags + clip tags."""
    title = (clip.get("title") or text_hook or "Clip").strip()[: config.TITLE_LIMIT]
    required = [t if t.startswith("#") else f"#{t}" for t in (campaign.get("hashtags") or "").split() if t]
    clip_tags = clip.get("hashtags") or []
    if isinstance(clip_tags, str):
        clip_tags = clip_tags.split()
    clip_tags = [t if t.startswith("#") else f"#{t}" for t in clip_tags][:5]
    seen, tags = set(), []
    for t in required + clip_tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    body = f"{text_hook.strip()}\n\n{' '.join(tags)}".strip()
    return title, body[: config.CAPTION_LIMITS.get(platform, 2000)]


def caption_file_text(platform: str, title: str, body: str, campaign: dict, clip: dict, variant_id: int) -> str:
    return "\n".join([
        f"PLATFORM: {platform}   POST WINDOW (ET): {config.POST_WINDOWS_ET.get(platform, 'any')}",
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
