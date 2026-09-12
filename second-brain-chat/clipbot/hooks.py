"""Alex's recorded hook lines. Drop audio files in iCloud Drive `ClipBot/hooks/` (Voice Memos →
share → Save to Files). Optional `manifest.json` maps filename → the spoken line; without it the
filename becomes the line ("wait_for_this_part.m4a" → "wait for this part"). Least-used first.
"""
from __future__ import annotations

import json
import os

from . import config
from .transform import probe_duration


def library(hooks_dir: str = config.HOOKS_DIR) -> list:
    if not os.path.isdir(hooks_dir):
        return []
    manifest = {}
    mp = os.path.join(hooks_dir, "manifest.json")
    if os.path.exists(mp):
        try:
            with open(mp) as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
    out = []
    for name in sorted(os.listdir(hooks_dir)):
        if not name.lower().endswith(config.AUDIO_EXT) or name.startswith("."):
            continue
        stem = os.path.splitext(name)[0]
        out.append({"file": os.path.join(hooks_dir, name),
                    "name": name,
                    "text": manifest.get(name) or manifest.get(stem) or stem.replace("_", " ").replace("-", " ").strip()})
    return out


def pick(hooks: list, uses: dict, exclude: set | None = None) -> dict | None:
    """Least-used hook, skipping any in `exclude` (so one clip's platform variants get different lines)."""
    exclude = exclude or set()
    cands = [h for h in hooks if h["name"] not in exclude] or hooks
    if not cands:
        return None
    return min(cands, key=lambda h: (uses.get(h["name"], 0), h["name"]))


def hook_length(hook: dict, cap: float) -> float:
    try:
        return min(cap, probe_duration(hook["file"]))
    except Exception:
        return 0.0
