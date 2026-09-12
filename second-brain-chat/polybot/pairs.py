"""Match Polymarket US markets to their offshore twins for lead-lag. Pure matching on titles and
end dates; the runner's `pairs` command feeds it the US list once the key exists and writes
`pairs.json` (= the leadlag module's universe)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from difflib import SequenceMatcher

from . import config

_STOP = {"will", "the", "a", "an", "of", "in", "on", "at", "to", "be", "by", "for", "vs", "vs.", "and", "or", "?"}


def normalize(title: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP)


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def match_pairs(us_markets: list, offshore_markets: list, threshold: float = 0.72, max_days_apart: float = 2.0) -> list:
    """us: [{slug, title, end, category?}] · offshore: [{token, title, end, category}] →
    [{us_slug, offshore_token, label, category, score}] best match per US market above threshold."""
    out = []
    off = [(m, normalize(m.get("title", "")), _date(m.get("end"))) for m in offshore_markets]
    for u in us_markets:
        nu, du = normalize(u.get("title", "")), _date(u.get("end"))
        best, best_score = None, 0.0
        for m, nm, dm in off:
            if du and dm and abs((du - dm).total_seconds()) > max_days_apart * 86400:
                continue
            score = SequenceMatcher(None, nu, nm).ratio()
            if score > best_score:
                best, best_score = m, score
        if best and best_score >= threshold:
            out.append({"us_slug": u["slug"], "offshore_token": best["token"], "label": u.get("title", u["slug"]),
                        "category": u.get("category") or best.get("category", "other"), "score": round(best_score, 3)})
    return out


def save_pairs(pairs: list, path: str | None = None) -> str:
    path = path or f"{config.ROOT}/pairs.json"
    with open(path, "w") as f:
        json.dump(pairs, f, indent=1)
    return path
