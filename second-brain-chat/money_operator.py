"""money_operator.py — the always-on driver that keeps Alex's money lanes moving.

Alex, 2026-09-16: "I just want something running on my hetzner server that prompts you/CLARVIS to
make me more money by continuing on the paths of all of the money making ideas, and then whenever
you finish the current task, it tells you to keep going and you find a new task to work on."

So this runs on the SERVER (always on) and owns the loop; the Mac is the hands:

  server tick (every 10 min)                        Mac (launchd capability watcher, every 10 min)
  ──────────────────────────                        ────────────────────────────────────────────
  settle the task in flight   ◄── money_task_update ── the worker reports done/blocked/failed + facts
  governor (runs per day, gap between runs)
  snapshot the three lanes
  ladder → the next unblocked task
  file it ── money_task row ─────────────────►      spawns `claude -p` with money_operator_rules.md
                                                    + the brief; the worker does the task, then reports

Policy lives in code (a deterministic ladder, caps, gates); judgment lives inside each task (the
worker is Claude Code with the repo, the skills, headless Chrome and the TikTok connector). Nothing
here calls a model, so the server side costs Supabase reads and nothing else.

What it will never do, by construction: send email (only the guarded queue script can), place or
arm trades, promote a polybot module, create accounts, or have two tasks in flight at once.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RULES_PATH = os.path.join(HERE, "money_operator_rules.md")

TASK_KIND = "money_task"
UPDATE_KIND = "money_task_update"
STATE_KEY = "operator:state"
SCOREBOARD_KEY = "business:operator"
FEATURE = "money_operator"                 # monitor.is_agent_allowed() name
TERMINAL = ("done", "blocked", "failed")
VALID_STATUSES = ("in_progress",) + TERMINAL
FIELD_CAP = 4000

TICK_SECONDS = 600
MAX_RUNS_PER_DAY = int(os.environ.get("MONEY_OPERATOR_MAX_RUNS", "10"))
MIN_GAP_MIN = int(os.environ.get("MONEY_OPERATOR_MIN_GAP_MIN", "20"))
IN_FLIGHT_TIMEOUT_MIN = 120                # a worker that started and never reported.
# 2026-09-17: was 75, which was SHORTER than the work actually takes. The sf_source worker
# picked up at 09:50 reported done at 11:26 (96 min, rc=0, six real brands in the tracker) —
# but the server had already marked it "worker timed out" at 11:05. A successful run recorded
# as a failure burns a governor slot, drops the facts, and makes the ladder repeat the work.
# The server must not give up while the Mac is still legitimately running the worker.
PICKUP_TIMEOUT_MIN = 120                   # nobody picked the task up (Mac asleep, watcher down)
PER_KIND_DAILY = {"sf_topup": 3, "clip_post": 3, "sf_hunter": 1, "sf_source": 2,
                  "poly_review": 1, "creator_list": 1, "whop_board": 1}
QUEUE_TARGET = 10                          # two release days of first touches in stock
POST_WINDOW = (17.0, 22.5)                 # local hours: the evening window the research points at
ACCOUNT_CREATED = date(2026, 9, 12)        # @wildest_moments
MIN_POST_GAP_S = 3 * 3600
HUNTER_PER_CYCLE = 25
HUNTER_CYCLE_DAY = 24                      # free-plan cycle resets around the 24th
QUIET_UNTIL_HOUR = 6                       # no new non-posting tasks before 06:00 local

supabase = None
intake = None
monitor = None
proactive = None
_sq_mod = None


def init(supabase_client, intake_mod=None, monitor_mod=None, proactive_mod=None) -> None:
    global supabase, intake, monitor, proactive
    supabase = supabase_client
    intake, monitor, proactive = intake_mod, monitor_mod, proactive_mod


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str, default=None):
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


# ------------------------------------------------------------------ the queue (Supabase rows)

class QueueError(RuntimeError):
    pass


def _rows(kind: str, limit: int = 80) -> list:
    if supabase is None:
        raise QueueError("money_operator has no Supabase client")
    try:
        res = (supabase.table("Agent Outputs").select("id,output_text,created_at")
               .eq("agent_name", kind).order("id", desc=True).limit(limit).execute())
    except Exception as e:
        raise QueueError(f"{kind} read failed: {e}") from e
    out = []
    for r in res.data or []:
        try:
            d = json.loads(r["output_text"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        d["_row_id"] = r.get("id")
        out.append(d)
    return out


def latest_update(slug: str) -> dict:
    for u in _rows(UPDATE_KIND):
        if u.get("slug") == slug:
            return u
    return {}


def pending_tasks() -> list:
    """Tasks with no terminal update yet, oldest first, each carrying its latest status."""
    out = []
    for t in _rows(TASK_KIND, limit=40):
        u = latest_update(t.get("slug", ""))
        if u.get("status") in TERMINAL:
            continue
        t["status"] = u.get("status", "pending")
        out.append(t)
    return list(reversed(out))


def file_task(task: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(LOCAL_TZ)
    slug = f"{task['kind']}-{now.strftime('%Y%m%d-%H%M')}"
    for t in pending_tasks():
        if t.get("kind") == task["kind"]:
            return t["slug"]               # never two of the same kind open at once
    payload = {"slug": slug, "kind": task["kind"], "lane": task.get("lane", ""),
               "title": task.get("title", "")[:200], "brief": task.get("brief", "")[:FIELD_CAP],
               "filed_at": _now_iso()}
    supabase.table("Agent Outputs").insert(
        {"agent_name": TASK_KIND, "output_text": json.dumps(payload)}).execute()
    return slug


def mark(slug: str, status: str, note: str = "", facts: dict | None = None) -> str:
    if status not in VALID_STATUSES:
        return f"Bad status {status!r} — use one of {VALID_STATUSES}."
    if supabase is None:
        return "No Supabase client."
    supabase.table("Agent Outputs").insert({
        "agent_name": UPDATE_KIND,
        "output_text": json.dumps({"slug": slug, "status": status, "note": (note or "")[:FIELD_CAP],
                                   "facts": facts or {}, "updated_at": _now_iso()}),
    }).execute()
    return f"Marked {slug} → {status}."


# ------------------------------------------------------------------ state

def _default_state() -> dict:
    return {"key": STATE_KEY, "paused": False, "in_flight": None, "runs": [],
            "kind_counts": {"date": "", "counts": {}}, "facts": {}, "posts": {},
            "done": {}, "fails": {}, "asks": [], "asks_nudged": None, "history": []}


def load_state() -> dict:
    st = _default_state()
    try:
        st.update(intake._load_state(STATE_KEY) or {})
    except Exception:
        pass
    st["key"] = STATE_KEY
    return st


def save_state(st: dict) -> None:
    st["key"] = STATE_KEY
    intake._save_state(st)


def _kind_counts(st: dict, today: str) -> dict:
    kc = st.setdefault("kind_counts", {"date": "", "counts": {}})
    if kc.get("date") != today:
        kc["date"], kc["counts"] = today, {}
    return kc["counts"]


# ------------------------------------------------------------------ the lanes' inputs

def _sq():
    """scripts/splitframe_queue.py, loaded by path (its pure functions know the tracker)."""
    global _sq_mod
    if _sq_mod is None:
        path = os.path.join(ROOT, "scripts", "splitframe_queue.py")
        spec = importlib.util.spec_from_file_location("splitframe_queue", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _sq_mod = mod
    return _sq_mod


def hunter_cycle_start(today: date) -> date:
    if today.day >= HUNTER_CYCLE_DAY:
        return today.replace(day=HUNTER_CYCLE_DAY)
    first = today.replace(day=1) - timedelta(days=1)
    return first.replace(day=HUNTER_CYCLE_DAY)


def hunter_used(rows: list, cycle_start: date) -> int:
    n = 0
    for r in rows:
        d = (r.get("email_checked") or "").strip()[:10]
        try:
            if date.fromisoformat(d) >= cycle_start:
                n += 1
        except ValueError:
            continue
    return n


def splitframe_inputs(today: date) -> dict:
    """What the ladder needs to know about the funnel. Fail-soft: the server reads the
    git-synced vault copy, and a missing tracker must not stop the other lanes."""
    out = {"ok": False, "reason": ""}
    try:
        sq = _sq()
        rows, _ = sq.tracker_rows()
        queue = list((intake._load_state(sq.QUEUE_KEY) or {}).get("queue") or [])
        pending = sq.pending_entries(queue)
        targets = sq.next_targets(rows, queue)
        hunters = sq.hunter_targets(rows)
        used = hunter_used(rows, hunter_cycle_start(today))
        out.update({
            "ok": True, "pending": len(pending),
            "draftable_in_band": sum(1 for t in targets if t["band"] in ("in", "unknown")),
            "hunter_targets_in_band": sum(1 for h in hunters if h["known_count"] is not None
                                          and sq.IN_BAND[0] <= h["known_count"] <= sq.IN_BAND[1]),
            "hunter_left": max(0, HUNTER_PER_CYCLE - used),
            "candidates_unread": len(sq.candidates_to_qualify(rows, today.isoformat(), limit=99)),
        })
    except Exception as e:                                   # noqa: BLE001
        out["reason"] = f"tracker/queue unreadable: {str(e)[:120]}"
    return out


def _lane_state(name: str) -> dict:
    try:
        return intake._load_state(f"business:{name}") or {}
    except Exception:
        return {}


def clip_inputs(st: dict, now: datetime) -> dict:
    lane = _lane_state("clipbot")
    stats = lane.get("stats") or {}
    if isinstance(stats, str):
        try:
            stats = json.loads(stats.replace("'", '"'))
        except ValueError:
            stats = {}
    variants = stats.get("variants") or {}
    age_days = (now.date() - ACCOUNT_CREATED).days
    return {"staged": int(variants.get("staged") or 0), "age_days": age_days,
            "cap": post_cap(age_days), "posted_today": int((st.get("posts") or {}).get(now.date().isoformat(), 0)),
            "last_post_ts": (st.get("facts") or {}).get("last_post_ts"),
            "lane_seen": lane.get("at") or lane.get("updated_at")}


def poly_inputs() -> dict:
    lane = _lane_state("polybot")
    return {"lane_seen": lane.get("at") or lane.get("updated_at"),
            "ready_to_promote": lane.get("ready_to_promote") or []}


def post_cap(age_days: int) -> int:
    if age_days < 1:
        return 0
    if age_days < 7:
        return 1
    if age_days < 21:
        return 2
    return 5


def snapshot(st: dict, now: datetime) -> dict:
    return {"splitframe": splitframe_inputs(now.date()), "clip": clip_inputs(st, now),
            "poly": poly_inputs(), "facts": st.get("facts") or {}, "done": st.get("done") or {},
            "idle_reasons": []}


# ------------------------------------------------------------------ the ladder (pure)

def next_task(snap: dict, now: datetime, counts: dict) -> dict | None:
    """First applicable rung wins. Every skipped rung leaves a reason in snap['idle_reasons']."""
    today = now.date().isoformat()
    reasons = snap.setdefault("idle_reasons", [])
    sf, clip, poly = snap.get("splitframe") or {}, snap.get("clip") or {}, snap.get("poly") or {}
    facts, done = snap.get("facts") or {}, snap.get("done") or {}

    def can(kind):
        if counts.get(kind, 0) >= PER_KIND_DAILY[kind]:
            reasons.append(f"{kind}: daily cap reached")
            return False
        return True

    def once_today(kind):
        if done.get(kind) == today:
            reasons.append(f"{kind}: done today")
            return False
        return can(kind)

    hour = now.hour + now.minute / 60
    quiet = hour < QUIET_UNTIL_HOUR

    # 1. keep the funnel stocked
    if sf.get("ok"):
        need = max(0, QUEUE_TARGET - sf["pending"])
        if need == 0:
            reasons.append("splitframe: queue full")
        elif sf["draftable_in_band"] == 0:
            reasons.append("splitframe: nothing in band left to draft")
        elif not quiet and can("sf_topup"):
            return _task("sf_topup", "splitframe", f"Top up the first-touch queue (+{min(5, need)})",
                         brief_sf_topup(sf, need))
    else:
        reasons.append(f"splitframe: {sf.get('reason') or 'no data'}")

    # 2. post a clip inside the evening window, at the account-safe cadence
    if clip.get("staged", 0) > 0:
        if POST_WINDOW[0] <= hour < POST_WINDOW[1]:
            last = clip.get("last_post_ts")
            gap_ok = not last or (now.timestamp() - float(last)) >= MIN_POST_GAP_S
            if clip["posted_today"] >= clip["cap"]:
                reasons.append(f"clip: cap {clip['cap']}/day reached")
            elif not gap_ok:
                reasons.append("clip: last post under 3 h ago")
            elif can("clip_post"):
                return _task("clip_post", "clipping", "Views, one TikTok post, Vyro submission",
                             brief_clip_post(clip))
        else:
            reasons.append("clip: outside the 17:00-22:30 posting window")
    else:
        reasons.append("clip: nothing staged")

    if quiet:
        reasons.append("quiet hours: nothing else before 06:00")
        return None

    # 3. verified addresses, inside the Hunter quota (once a day)
    hunter_possible = False
    if sf.get("ok"):
        if sf["hunter_left"] <= 0:
            reasons.append("hunter: quota spent this cycle")
        elif sf["hunter_targets_in_band"] <= 0:
            reasons.append("hunter: no in-band brand is waiting for a person")
        elif done.get("sf_hunter") == today:
            reasons.append("sf_hunter: done today")
        else:
            hunter_possible = True
    if hunter_possible and can("sf_hunter"):
        return _task("sf_hunter", "splitframe", "Find founder emails for in-band brands", brief_sf_hunter(sf))

    # 4. nothing to draft and Hunter cannot help right now: source new in-band brands
    if sf.get("ok") and sf["draftable_in_band"] == 0 and not hunter_possible and can("sf_source"):
        return _task("sf_source", "splitframe", "Source new in-band brands from the Ad Library",
                     brief_sf_source(sf))

    # 5. polybot engineering, once a day
    if once_today("poly_review"):
        return _task("poly_review", "polybot", "Daily polybot review, one improvement", brief_poly_review(poly))

    # 6. the creator-retainer list, once a day
    if once_today("creator_list"):
        return _task("creator_list", "creator", "Five creator-retainer prospects", brief_creator_list())

    # 7. the Whop board, once a day, only when the operator profile is logged in
    if facts.get("whop_logged_in"):
        if once_today("whop_board"):
            return _task("whop_board", "clipping", "Read the Whop Content Rewards board", brief_whop_board())
    else:
        reasons.append("whop: operator profile not logged in")
    return None


def _task(kind, lane, title, brief) -> dict:
    return {"kind": kind, "lane": lane, "title": title, "brief": brief}


# ------------------------------------------------------------------ briefs

def brief_sf_topup(sf: dict, need: int) -> str:
    return (f"The first-touch queue holds {sf['pending']} of the {QUEUE_TARGET} it should (5 release a "
            f"day). Queue up to {min(5, need)} new first touches. Run `python3 scripts/splitframe_queue.py "
            f"status`; work 'Can be drafted next' top to bottom, in-band brands first. For each brand: live "
            f"read with `python3 scripts/adlib_read.py --page-id <id>` (0 active → `note --ad-count 0`; over "
            f"100 → `note` and skip), write the email in Alex's voice with the splitframe-outreach skill from "
            f"what you just read, save the body to a temp file, then `splitframe_queue.py add ... --ad-count N "
            f"--evidence \"...\"`. Never work around the script. Report facts drafts_queued=<n>.")


def brief_sf_hunter(sf: dict) -> str:
    n = min(8, sf["hunter_left"], sf["hunter_targets_in_band"])
    return (f"Hunter has {sf['hunter_left']} searches left this cycle and {sf['hunter_targets_in_band']} "
            f"in-band qualified brands have no person's address. Run `python3 scripts/fill_contacts.py "
            f"--brands \"<up to {n} in-band brands from 'Next Hunter window' in splitframe_queue.py status>\" "
            f"--limit {n}`. Never re-search a brand that already has an email_checked date; shared inboxes "
            f"are never targets. Report facts hunter_searched=<n>, people_found=<n>.")


def brief_sf_source(sf: dict) -> str:
    return ("The tracker is mined out: every remaining prospect is either a 100+ ad in-house team or a dead "
            "candidate row (0 active ads). The funnel needs NEW brands that are spending right now in the 5-50 "
            "active-ad band. Use Ad Library keyword searches (`python3 scripts/adlib_read.py --keyword "
            "\"<category>\"`, categories like pet supplies, skincare, supplements, swimwear, snacks, home goods, "
            "activewear; DTC brands only, never retailers, agencies or marketplaces), pick advertisers whose "
            "own page (`--page-id`) shows 5-50 active ads, and add each with `python3 scripts/splitframe_queue.py "
            "source --brand \"...\" --domain ... --ad-count N --page-id ... --category ... --evidence \"keyword "
            "<q>: what the ads showed\"`. Target 6 new in-band brands this run. Emails come later from Hunter. "
            "Report facts candidates_added=<n>.")


def brief_clip_post(clip: dict) -> str:
    last = clip.get("last_post_ts")
    ago = "never" if not last else f"{int((datetime.now(timezone.utc).timestamp() - float(last)) / 60)} min ago"
    return (f"@wildest_moments is {clip['age_days']} days old → cap {clip['cap']} post(s) a day; {clip['posted_today']} "
            f"posted today by this operator's count; last post {ago}; {clip['staged']} clips staged. Before "
            f"posting, count today's posts from the clipbot ledger itself (`python3 -m clipbot.runner report`, "
            f"or the newest files in ClipBot/ready/_posted/): the ledger wins over this count, and the cap and "
            f"the 3 h gap apply to everything posted today by anyone. In order: (1) record views for every "
            f"post at least a day old (`curl -sL <url>` → \"playCount\" → `python3 -m clipbot.runner views`); "
            f"(2) if a slot is open and it is between 17:00 and 22:30 ET, post ONE TikTok clip from the POST "
            f"ORDER (`python3 -m clipbot.runner plan`), transformation-safe campaigns only, through the Higgsfield "
            f"connector exactly as the rules describe, then `python3 -m clipbot.runner posted --variant N --url "
            f"<url>`; (3) try to submit every posted-but-unsubmitted URL on Vyro with the operator Chrome "
            f"profile (a login wall → facts vyro_logged_in=false and an ask). Report facts posted_now=<0|1>, "
            f"last_post_ts=<unix seconds of the post, if any>, views_total=<n>, vyro_logged_in=<true|false>.")


def brief_poly_review(poly: dict) -> str:
    ready = poly.get("ready_to_promote") or []
    return ("Daily polybot review (paper only). Source .env, then `python3 -m polybot.runner status`, "
            "`report --days 7`, `tail -200 polybot/loop.log`, `launchctl list | grep polybot`. Dead or erroring "
            "loop → fix and reload. Read the ledger like an engineer (signals/day per module since gate_since_ts, "
            "fill rate, closed net, mark-to-market, US paper record) against `backtest --days 7`. Open question: "
            "weather_lock backtests +7.5% over 180 signals but live paper shows very few signals and no fills — "
            "find out why and fix a defect if it is one. ONE concrete improvement with a test, tests green, "
            "restart the loop, commit only the files you touched, never push. "
            + (f"Modules ready to promote: {ready} — put the exact one-line go-live edit in NEEDS_ALEX; never make it. "
               if ready else "")
            + "Report facts poly_note=<one line>.")


def brief_creator_list() -> str:
    return ("Creator-retainer lane (same outreach machine, new list): find up to 5 NEW streamers or podcasters "
            "with a devoted audience (roughly 2-8k average live viewers, or a real podcast following), little "
            "existing clip coverage, and a business email public on a YouTube About page or bio. Append them to "
            "\"<Money folder>/Creator Lane — Prospects.md\" (dedupe by name). Send nothing and add nothing to the "
            "tracker until \"Creator Lane — Offer (approved).md\" exists; if neither it nor \"Creator Lane — Offer "
            "(proposed).md\" exists, write the proposed offer once. Report facts creators_added=<n>.")


def brief_whop_board() -> str:
    return ("Read the Whop Content Rewards board with the operator Chrome profile (a login wall → facts "
            "whop_logged_in=false and an ask). List the top 3 open campaigns that allow recuts, hooks and captions, "
            "sorted by REMAINING budget then rate, in the log. If one fits and footage is reachable, create the "
            "clipbot campaign row with its rules; a footage download that needs approval is an ask. "
            "Report facts whop_top=<one line>.")


# ------------------------------------------------------------------ governor + tick

def runs_in_last_24h(st: dict, now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    n = 0
    for r in st.get("runs") or []:
        dt = _parse(r)
        if dt and dt >= cutoff:
            n += 1
    return n


def governor_blocks(st: dict, now: datetime) -> str | None:
    if runs_in_last_24h(st, now) >= MAX_RUNS_PER_DAY:
        return f"{MAX_RUNS_PER_DAY} runs in the last 24 h (usage cap)"
    runs = st.get("runs") or []
    last = _parse(runs[-1]) if runs else None
    if last and (now - last) < timedelta(minutes=MIN_GAP_MIN):
        return f"last task filed {int((now - last).total_seconds() // 60)} min ago (gap {MIN_GAP_MIN} min)"
    return None


def apply_update(st: dict, inf: dict, u: dict, now: datetime) -> None:
    kind, status = inf.get("kind", ""), u.get("status")
    today = now.date().isoformat()
    facts = u.get("facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    st.setdefault("facts", {})
    for k, v in facts.items():
        if k == "posted_now":
            try:
                n = int(v or 0)
            except (TypeError, ValueError):
                n = 0
            st.setdefault("posts", {})[today] = st.get("posts", {}).get(today, 0) + n
        elif k == "asks":
            if isinstance(v, list):
                st["asks"] = [str(a)[:300] for a in v][:8]
        else:
            st["facts"][k] = v
    if status in ("done", "blocked"):
        st.setdefault("done", {})[kind] = today
    elif status == "failed":
        key = f"{today}:{kind}"
        st.setdefault("fails", {})[key] = st.get("fails", {}).get(key, 0) + 1
        if st["fails"][key] >= 2:
            st.setdefault("done", {})[kind] = today
    st.setdefault("history", []).append({
        "slug": inf.get("slug"), "kind": kind, "status": status,
        "note": (u.get("note") or "")[:240], "at": now.isoformat()})
    st["history"] = st["history"][-30:]
    _nudge_asks(st, now)


def _nudge_asks(st: dict, now: datetime) -> None:
    asks = st.get("asks") or []
    today = now.date().isoformat()
    if not asks or not proactive or st.get("asks_nudged") == today:
        return
    if st.get("asks_seen") == asks:
        return
    try:
        proactive.send_nudge("money-operator-asks",
                             f"Money operator: {len(asks)} thing{'s' if len(asks) > 1 else ''} only you can do",
                             asks[0] + (f" (+{len(asks) - 1} more in NEEDS_ALEX)" if len(asks) > 1 else ""),
                             priority="default", tags="moneybag")
        st["asks_nudged"], st["asks_seen"] = today, list(asks)
    except Exception:
        pass


def settle_in_flight(st: dict, now: datetime) -> None:
    inf = st.get("in_flight")
    if not inf:
        return
    u = latest_update(inf["slug"])
    status = u.get("status")
    if status in TERMINAL:
        apply_update(st, inf, u, now)
        st["in_flight"] = None
        return
    filed = _parse(inf.get("filed_at")) or now
    if status == "in_progress":
        started = _parse(u.get("updated_at")) or filed
        if now - started > timedelta(minutes=IN_FLIGHT_TIMEOUT_MIN):
            mark(inf["slug"], "failed", f"worker ran past {IN_FLIGHT_TIMEOUT_MIN} min without reporting")
            apply_update(st, inf, {"status": "failed", "note": "worker timed out"}, now)
            st["in_flight"] = None
        return
    if now - filed > timedelta(minutes=PICKUP_TIMEOUT_MIN):
        mark(inf["slug"], "failed", f"no worker picked it up in {PICKUP_TIMEOUT_MIN} min "
                                    "(Mac asleep or the capability watcher is not running)")
        apply_update(st, inf, {"status": "failed", "note": "never picked up"}, now)
        st["in_flight"] = None
        today = now.date().isoformat()
        if proactive and st.get("pickup_nudged") != today:
            try:
                proactive.send_nudge("money-operator-pickup", "The money operator has no hands",
                                     "The server has been filing work but your Mac has not picked any of it up "
                                     "for 2 hours. Open the laptop, or check the capability watcher "
                                     "(python3 scripts/watcher_health.py).", priority="high", tags="warning")
                st["pickup_nudged"] = today
            except Exception:
                pass


def _publish(st: dict, now: datetime, note: str) -> None:
    if not intake:
        return
    try:
        sb = intake._load_state(SCOREBOARD_KEY) or {}
        sb.update({"key": SCOREBOARD_KEY, "at": now.isoformat(), "note": note[:300],
                   "in_flight": st.get("in_flight"), "runs_24h": runs_in_last_24h(st, now),
                   "asks": st.get("asks") or [], "paused": bool(st.get("paused")),
                   "last": (st.get("history") or [{}])[-1]})
        intake._save_state(sb)
    except Exception:
        pass
    if monitor:
        try:
            monitor.beat("money-operator", stale_after_s=4 * TICK_SECONDS, note=note[:120])
        except Exception:
            pass


def tick(now: datetime | None = None, dry_run: bool = False) -> dict:
    """One decision. Returns what happened, for the log and the tests."""
    now = now or datetime.now(LOCAL_TZ)
    st = load_state()
    if st.get("paused") or os.environ.get("MONEY_OPERATOR", "1") == "0":
        _publish(st, now, "paused")
        return {"idle": "paused"}
    if monitor and not monitor.is_agent_allowed(FEATURE):
        _publish(st, now, "budget tier blocks automated work")
        return {"idle": "budget"}
    settle_in_flight(st, now)
    if st.get("in_flight"):
        save_state(st)
        _publish(st, now, f"waiting on {st['in_flight']['slug']}")
        return {"waiting": st["in_flight"]["slug"]}
    block = governor_blocks(st, now)
    if block:
        save_state(st)
        _publish(st, now, f"idle: {block}")
        return {"idle": block}
    snap = snapshot(st, now)
    counts = _kind_counts(st, now.date().isoformat())
    task = next_task(snap, now, counts)
    if not task:
        save_state(st)
        why = "; ".join(snap.get("idle_reasons") or ["nothing to do"])
        _publish(st, now, f"idle: {why}"[:300])
        return {"idle": why}
    if dry_run:
        return {"would_file": task, "idle_reasons": snap.get("idle_reasons")}
    slug = file_task(task, now)
    st["in_flight"] = {"slug": slug, "kind": task["kind"], "filed_at": now.isoformat()}
    st.setdefault("runs", []).append(now.isoformat())
    st["runs"] = st["runs"][-60:]
    counts[task["kind"]] = counts.get(task["kind"], 0) + 1
    save_state(st)
    _publish(st, now, f"filed {slug}: {task['title']}")
    return {"filed": slug, "task": task}


def loop() -> None:
    import time
    while True:
        try:
            tick()
        except Exception as e:                                # noqa: BLE001
            if monitor:
                try:
                    monitor.report_event("money-operator", "warning", "tick failed", str(e)[:300])
                except Exception:
                    pass
        time.sleep(TICK_SECONDS)


# ------------------------------------------------------------------ CLARVIS tool + CLI

def status_text() -> str:
    st = load_state()
    now = datetime.now(LOCAL_TZ)
    lines = [f"Money operator — {'PAUSED' if st.get('paused') else 'running'}; "
             f"{runs_in_last_24h(st, now)}/{MAX_RUNS_PER_DAY} runs in the last 24 h."]
    inf = st.get("in_flight")
    lines.append(f"In flight: {inf['slug']} (filed {inf['filed_at'][:16]})" if inf else "In flight: nothing.")
    for h in (st.get("history") or [])[-5:]:
        lines.append(f"  {h.get('at', '')[:16]} {h.get('kind')}: {h.get('status')} — {h.get('note', '')[:120]}")
    if st.get("asks"):
        lines.append("Only Alex can do: " + " | ".join(st["asks"][:4]))
    return "\n".join(lines)


TOOL_SCHEMAS = [{
    "name": "money_operator",
    "description": "The always-on money operator that keeps Splitframe, clipping and polybot moving without "
                   "Alex. status = what it is doing now, what it did last, what only Alex can do; pause / resume it.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "pause", "resume"]}}},
}]
TOOL_STATUS_LABELS = {"money_operator": "Checking the money operator…"}


def handle_tool_call(name: str, tool_input: dict) -> str:
    if name != "money_operator":
        return "Unknown money operator tool."
    action = (tool_input or {}).get("action", "status")
    if action in ("pause", "resume"):
        st = load_state()
        st["paused"] = action == "pause"
        save_state(st)
        return f"Money operator {'paused' if st['paused'] else 'resumed'}."
    return status_text()


def main(argv=None) -> int:
    """CLI shared by the Mac worker (report) and by humans (status / dry run)."""
    import argparse
    ap = argparse.ArgumentParser(description="money operator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("pending")
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    for s in ("done", "blocked", "failed", "in_progress"):
        p = sub.add_parser(s, help=f"report a task as {s}")
        p.add_argument("--slug", required=True)
        p.add_argument("--note", default="")
        p.add_argument("--facts", default="{}", help="JSON object of facts for the ladder")
    sub.add_parser("pause")
    sub.add_parser("resume")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(status_text())
    elif a.cmd == "pending":
        for tk in pending_tasks():
            print(f"{tk['slug']} [{tk.get('status')}] {tk.get('title')}")
    elif a.cmd == "tick":
        print(json.dumps(tick(dry_run=a.dry_run), indent=1, default=str))
    elif a.cmd in ("pause", "resume"):
        print(handle_tool_call("money_operator", {"action": a.cmd}))
    else:
        try:
            facts = json.loads(a.facts or "{}")
        except ValueError:
            print("--facts must be a JSON object")
            return 2
        print(mark(a.slug, a.cmd, a.note, facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
