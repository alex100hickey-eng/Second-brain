#!/usr/bin/env python3
"""Splitframe reply watcher — the one thing that must never sit unseen.

Every run: read the studio inbox for mail from any prospect domain in the tracker (or any
address the tracker has a `sent_date` for), and for each new one:
  - nudge Alex's phone (ntfy, via CLARVIS send_nudge when importable)
  - stamp `replied` in `Money/prospect-tracker.csv` (backup written first)
  - append to scripts/reply_watch.log
Read-only on Gmail. Never sends. Runs every 30 min under launchd (com.secondbrain.replywatch).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime

VAULT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
STATE = os.path.expanduser("~/second-brain/scripts/reply_watch_state.json")
LOG = os.path.expanduser("~/second-brain/scripts/reply_watch.log")
OWN = {"splitframestudio.com", "gmail.com", "google.com", "hunter.io", "stripe.com", "icloud.com"}


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": []}


def save_state(st: dict) -> None:
    with open(STATE, "w") as f:
        json.dump(st, f)


def tracker_rows() -> list:
    with open(TRACKER, newline="") as f:
        return list(csv.DictReader(f))


def prospect_domains(rows) -> dict:
    """domain -> brand for every tracker row (a reply can come from anyone at the brand)."""
    out = {}
    for r in rows:
        d = (r.get("domain") or "").strip().lower().removeprefix("www.")
        if d:
            out[d] = r.get("brand") or d
        e = (r.get("email") or "").strip().lower()
        if "@" in e:
            out[e.split("@", 1)[1]] = r.get("brand") or d
    return out


def stamp_replied(brand: str, when: str) -> None:
    rows = tracker_rows()
    fields = list(rows[0].keys())
    changed = False
    for r in rows:
        if r.get("brand") == brand and not (r.get("replied") or "").strip():
            r["replied"] = when
            changed = True
    if not changed:
        return
    shutil.copy2(TRACKER, TRACKER + f".bak-replywatch-{when}")
    with open(TRACKER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def nudge(title: str, body: str) -> None:
    sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
    try:
        import proactive  # type: ignore
        reason = proactive.send_nudge("splitframe-reply", title, body, priority="high", tags="incoming_envelope", force=True)
        if not reason:
            return
        log(f"send_nudge refused: {reason}")
    except Exception as exc:
        log(f"send_nudge unavailable: {exc}")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    import urllib.request
    req = urllib.request.Request(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}", data=body.encode(),
                                 headers={"Title": title[:120], "Priority": "high", "Tags": "incoming_envelope"})
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    from composio import Composio  # type: ignore
    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    ent = os.environ.get("STUDIO_GMAIL_ENTITY")
    rows = tracker_rows()
    domains = prospect_domains(rows)
    st = load_state()
    seen = set(st.get("seen", []))
    res = c.tools.execute("GMAIL_FETCH_EMAILS", user_id=ent, dangerously_skip_version_check=True,
                          arguments={"query": "in:inbox newer_than:14d", "max_results": 30})
    msgs = (res.get("data") or {}).get("messages") or []
    hits = 0
    for m in msgs:
        mid = m.get("messageId") or m.get("id")
        sender = str(m.get("sender") or "")
        addr = sender.split("<")[-1].rstrip(">").strip().lower()
        dom = addr.split("@", 1)[1] if "@" in addr else ""
        if not mid or mid in seen or not dom or dom in OWN:
            continue
        brand = domains.get(dom)
        if not brand:
            continue
        subject = str(m.get("subject") or "")[:80]
        preview = str(m.get("preview") or m.get("snippet") or "")
        if isinstance(m.get("preview"), dict):
            preview = str(m["preview"].get("body") or "")
        when = datetime.now().strftime("%Y-%m-%d")
        log(f"REPLY from {brand} <{addr}>: {subject}")
        stamp_replied(brand, when)
        nudge(f"{brand} replied", f"{sender}: {subject}\n{preview[:180]}\nReply today. Call card: Money/call-card.md")
        seen.add(mid)
        hits += 1
    st["seen"] = sorted(seen)[-500:]
    st["last_run"] = datetime.now().isoformat()
    save_state(st)
    if not hits:
        log(f"no prospect replies ({len(msgs)} inbox messages scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
