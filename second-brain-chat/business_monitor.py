"""business_monitor.py — one watcher over all three of Alex's money lanes.

Liveness monitoring already exists (monitor.check_heartbeats). This is the layer above it, and it
exists because every failure this month was a HEALTHY process doing nothing useful:

  * clipbot beat happily for two days while 187 finished clips sat unposted.
  * splitframe drafted into Gmail while zero outbox rows meant no nudge could ever fire.
  * polybot scanned all night while every module lost money in paper.
  * @wildest_moments posted 13 videos to an account with no reach and nobody noticed for 3 days.

So this does not ask "is it running". It asks "is it earning, and what is stuck". Runs on the
always-on server; the Mac loops publish their own scoreboards into shared state (`business:<lane>`)
because the server cannot read the Mac's sqlite.

Read-only and nudge-only. It never sends, trades, posts, or promotes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

intake = None
outbox = None

LANES = ("splitframe", "polybot", "clipbot")
STALE_LANE_H = 6            # a lane that hasn't published in this long is not reporting


def init(intake_mod, outbox_mod=None):
    global intake, outbox
    intake, outbox = intake_mod, outbox_mod


def _age_h(iso: str | None):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt).total_seconds() / 3600


def _lane(name: str) -> dict:
    try:
        return intake._load_state(f"business:{name}") or {}
    except Exception:
        return {}


def splitframe_status() -> dict:
    """The only lane with a real path to revenue, so its failure modes get the most attention."""
    out = {"lane": "splitframe", "problems": [], "line": ""}
    try:
        queue = (intake._load_state("splitframe:firsttouch_queue") or {}).get("queue") or []
        pending = [e for e in queue if not e.get("released")]
        waiting, approved = [], []
        if outbox:
            waiting = [i for i in outbox.open_items() if i.get("kind") == "email_draft"]
            approved = [i for i in waiting if i.get("send_approved") or i.get("auto_send_at")]
        out["queue_pending"] = len(pending)
        out["waiting"] = len(waiting)
        out["line"] = f"{len(pending)} drafted & queued, {len(waiting)} waiting on the phone"

        if not pending and not waiting:
            out["problems"].append("the funnel is EMPTY — no drafted first touches left, "
                                   "so nothing goes out tomorrow")
        for it in approved:
            age = _age_h(it.get("send_approved") or it.get("auto_send_at"))
            if age and age > 6:
                out["problems"].append(
                    f"an approved email to {(it.get('title') or '').split()[-1]} has been stuck "
                    f"{int(age)}h — the Mac has not been awake to send it")
                break
    except Exception as e:
        out["problems"].append(f"splitframe status unreadable: {str(e)[:80]}")
    return out


def polybot_status() -> dict:
    out = {"lane": "polybot", "problems": [], "line": ""}
    st = _lane("polybot")
    if not st:
        out["problems"].append("polybot has never published a scoreboard")
        return out
    age = _age_h(st.get("at"))
    live = [m for m, mode in (st.get("modes") or {}).items() if mode == "live"]
    ready = st.get("ready_to_promote") or []
    out["line"] = (f"${st.get('bankroll_usd', '?')} bankroll · "
                   f"{len(live)} module(s) live · {st.get('live_orders', 0)} real orders")
    if age and age > STALE_LANE_H:
        out["problems"].append(f"polybot has not reported in {int(age)}h — the Mac loop may be dead")
    if ready:
        # The one thing here that genuinely needs him: arming real money is his call.
        out["problems"].append(f"READY TO GO LIVE: {', '.join(ready)} passed the gate. "
                               "Say the word and it trades real money.")
    return out


def clipbot_status() -> dict:
    out = {"lane": "clipbot", "problems": [], "line": ""}
    st = _lane("clipbot")
    if not st:
        out["problems"].append("clipbot has never published a scoreboard")
        return out
    stats = st.get("stats") or {}
    posts, views = stats.get("posts", 0), stats.get("views", 0)
    out["line"] = f"{posts} posts · {views} views · ${stats.get('expected_usd', 0):.2f} expected"
    age = _age_h(st.get("at"))
    if age and age > STALE_LANE_H:
        out["problems"].append(f"clipbot has not reported in {int(age)}h")
    if st.get("risky_campaigns"):
        out["problems"].append(
            "campaign(s) that forbid transformation are still active: "
            f"{', '.join(st['risky_campaigns'])} — posting these is what killed the account's reach")
    if posts >= 5 and views < posts * 100:
        out["problems"].append(
            f"reach is dead: {views} views across {posts} posts. Posting more earns nothing "
            "until the account recovers or moves to original content.")
    return out


def snapshot() -> dict:
    return {"splitframe": splitframe_status(),
            "polybot": polybot_status(),
            "clipbot": clipbot_status()}


def problems(snap: dict | None = None) -> list:
    snap = snap or snapshot()
    return [(s["lane"], p) for s in snap.values() for p in s["problems"]]


def digest(snap: dict | None = None) -> str:
    snap = snap or snapshot()
    return "\n".join(f"{s['lane']}: {s['line'] or 'no data'}" for s in snap.values())
