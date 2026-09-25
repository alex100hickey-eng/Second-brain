"""Paths, per-platform variant recipes, caps. Secrets from the environment only.

Env:
    OPUSCLIP_API_KEY     from https://clip.opus.pro/dashboard (Pro/Max/Enterprise)
    CLIPBOT_HOME         working storage for downloads/variants (default ~/Movies/clipbot)
    CLIPBOT_READY_DIR    where finished clips are staged (default iCloud Drive ClipBot/ready)
    CLIPBOT_HOOKS_DIR    Alex's recorded hook lines (default iCloud Drive ClipBot/hooks)
    NTFY_TOPIC           already in ~/second-brain/.env; the daily "clips ready" nudge uses it
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.abspath(__file__))
ICLOUD = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
HOME = os.environ.get("CLIPBOT_HOME", os.path.expanduser("~/Movies/clipbot"))
READY_DIR = os.environ.get("CLIPBOT_READY_DIR", os.path.join(ICLOUD, "ClipBot", "ready"))
HOOKS_DIR = os.environ.get("CLIPBOT_HOOKS_DIR", os.path.join(ICLOUD, "ClipBot", "hooks"))
INBOX_DIR = os.environ.get("CLIPBOT_INBOX_DIR", os.path.join(ICLOUD, "ClipBot", "inbox"))
# Raw source videos (VOD sections, campaign originals) live here, NOT in iCloud.
SOURCES_DIR = os.environ.get("CLIPBOT_SOURCES_DIR", os.path.expanduser("~/ClipBot-sources"))
# Disk floor. Below this much free space macOS starts evicting iCloud files, and on 2026-09-24 it evicted the
# whole vault three times (523 files, 13 held follow-ups). No new source download starts under it, and a
# source whose clips are all rendered is deleted (its re-fetch recipe is kept in the ledger).
MIN_FREE_GB = float(os.environ.get("CLIPBOT_MIN_FREE_GB", "15"))
DB_PATH = os.environ.get("CLIPBOT_DB", os.path.join(ROOT, "clipbot.db"))
CONFIG_PATH = os.environ.get("CLIPBOT_CONFIG", os.path.join(ROOT, "config.json"))
KILL_PATH = os.path.join(ROOT, "KILL")
FONT = os.environ.get("CLIPBOT_FONT", "/System/Library/Fonts/Supplemental/Arial Black.ttf")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".aac", ".caf")

PLATFORMS = ["tiktok", "shorts", "reels", "facebook"]

# Distinct trims / text placement / zoom per platform, so no two uploads are byte-identical
# (Whop's fraud rules and the platforms' duplicate detection both punish identical copies).
VARIANTS = {
    "tiktok":   {"trim_start": 0.0, "trim_end": 0.0, "text_pos": "top",    "zoom": 1.00},
    "shorts":   {"trim_start": 0.3, "trim_end": 0.2, "text_pos": "middle", "zoom": 1.03},
    "reels":    {"trim_start": 0.6, "trim_end": 0.0, "text_pos": "top",    "zoom": 1.02},
    "facebook": {"trim_start": 0.2, "trim_end": 0.4, "text_pos": "middle", "zoom": 1.00},
}

# Posting windows (Eastern) written into each caption file. Generic 2026 guidance, tune from stats.
POST_WINDOWS_ET = {"tiktok": "7:00–9:30 PM", "shorts": "3:00–5:00 PM",
                   "reels": "11:00 AM–1:00 PM", "facebook": "12:00–2:00 PM"}

CAPTION_LIMITS = {"tiktok": 2200, "shorts": 4900, "reels": 2200, "facebook": 2000}
TITLE_LIMIT = 95


# Per-campaign brief rules, stored as json in campaigns.rules. Every Vyro/Whop brief differs on these.
DEFAULT_RULES = {
    "voice": True,          # False: no voice-hook audio (briefs that say "do not change the audio")
    "text_hook": True,      # False: no on-screen hook card (briefs that forbid added text/subtitles)
    "extra_tags": True,     # False: only the campaign's required hashtags, none from the clip
    "caption": "",          # mandatory caption line the brief requires, verbatim
    "tag": "",              # account to tag in the caption, e.g. "@adultsfx"
    "min_seconds": 0.0,     # clips shorter than this are skipped (Vyro TV briefs: 30)
    "max_seconds": 0.0,     # clips longer than this are skipped (0 = no cap)
    "durations": None,      # OpusClip clipDurations override, e.g. [[30, 60]]; None = derived/min-max or config
    "brand_template_id": "",  # OpusClip brand template for this campaign (captions on/off live there)
    "direct": False,        # True: inbox files are pre-cut clips; skip OpusClip (0 credits), transform + stage as-is
    "hook_lines": [],       # brief-approved caption/on-screen lines; rotate through these instead of OpusClip's titles
    # Text a brief REQUIRES on screen (Double Date Island: "Use 'Double Date Island' as on-screen
    # text somewhere in your clip"). Unlike the hook card this stays up for the whole clip, because
    # it is a compliance element: a rotating hook that happens to omit it makes the clip rejectable,
    # and a rejected clip earns nothing however well it performs.
    "required_text": "",
    # Path to a brand logo a brief REQUIRES watermarked on the clip (Crazy Taxi: "Add the official
    # Crazy Taxi: World Tour logo as a watermark"). Rendering refuses if the file is missing.
    "required_logo": "",
    # Per-platform replacement for `caption`, e.g. {"shorts": "...@TorteDeLini @SEGA_West...", "reels": "..."}.
    # Crazy Taxi tags a different official account and credits the streamer differently on YouTube
    # than on TikTok/Instagram, so one caption line cannot satisfy the brief on all three.
    "captions": {},
    "ends": "",             # campaign end date YYYY-MM-DD; the post-order list puts the soonest deadline first
    "per_day": 3,           # how many of this campaign's clips to post per day (a new account gets throttled past ~3)
}


def campaign_rules(campaign) -> dict:
    raw = (campaign or {}).get("rules") if isinstance(campaign, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    out = dict(DEFAULT_RULES)
    out.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_RULES})
    return out


@dataclass
class Config:
    weekly_credit_budget: int = 300          # 5 source hours a week → 12 weeks of runway
    monthly_api_cap: int = 900               # OpusClip Pro (Beta) API cap; /api-usage is the truth
    clip_durations: list = field(default_factory=lambda: [[15, 35], [30, 60]])
    model: str = "ClipAnything"
    aspect: str = "portrait"
    min_score: float = 0.0                   # OpusClip virality score floor; 0 keeps everything
    max_clips_per_source: int = 12
    platforms: list = field(default_factory=lambda: list(PLATFORMS))
    hook_seconds_max: float = 4.0
    text_hook_seconds: float = 2.8
    poster: str = "folder"                   # folder (Alex posts from Files) | opus (OpusClip scheduler)
    opus_privacy: str = "public"
    nudge_hour_et: int = 17
    brand_template_id: str = ""
    poll_minutes: int = 5


def load(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
    return cfg


def save(cfg: Config, path: str = CONFIG_PATH) -> None:
    with open(path, "w") as f:
        json.dump(asdict(cfg), f, indent=2)


def api_key_present() -> bool:
    return bool(os.environ.get("OPUSCLIP_API_KEY"))


def kill_switch_on() -> bool:
    return os.path.exists(KILL_PATH)


def ensure_dirs() -> None:
    for d in (HOME, os.path.join(HOME, "hd"), os.path.join(HOME, "variants"), READY_DIR, HOOKS_DIR, INBOX_DIR):
        os.makedirs(d, exist_ok=True)
    for p in PLATFORMS:
        os.makedirs(os.path.join(READY_DIR, p), exist_ok=True)
