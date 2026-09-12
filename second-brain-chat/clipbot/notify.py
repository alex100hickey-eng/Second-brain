"""The one nudge clipbot sends: "N clips are ready in Files". Goes through CLARVIS's send_nudge
(respect rules: quiet hours, daily cap) when importable, else straight to ntfy. Never raises."""
from __future__ import annotations

import os


def nudge(title: str, body: str, key: str = "clipbot-ready", log=print) -> bool:
    try:
        import proactive  # CLARVIS module; same directory when run from second-brain-chat
        reason = proactive.send_nudge(key, title, body, priority="default", tags="clapper")
        log(f"  nudge via send_nudge: {reason or 'sent'}")
        return not reason
    except Exception as exc:
        log(f"  send_nudge unavailable ({exc}); falling back to ntfy")
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        log("  no NTFY_TOPIC; nudge skipped")
        return False
    try:
        import urllib.request
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
        req = urllib.request.Request(f"{server}/{topic}", data=body.encode(),
                                     headers={"Title": title[:120], "Priority": "default", "Tags": "clapper"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as exc:
        log(f"  ntfy failed: {exc}")
        return False
