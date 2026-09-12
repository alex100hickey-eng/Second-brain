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
