"""
daily_orders.py — one ranked list of what today is actually for.

Why this exists: CLARVIS already knows the school deadlines (school_data), the
training week (training_sync), and the money plan (august_tracker +
ad_creative_pipeline) — but each answer lives behind its own tool, so "what
should I do today" meant four tool calls and a synthesis Alex had to do in his
head. This module IS the synthesis: compose() pulls every pillar, ranks by
consequence (hard deadline > exam runway > money follow-ups > training > prep >
hygiene), and returns at most 8 orders where every "why" is one concrete clause
— points, streak, deadline — never motivational fluff.

The scorecard closes the loop: one nightly log of school/ball/money/sleep as
booleans, so streaks are computed facts CLARVIS can cite, not vibes.

Design rules (same as every subsystem):
  * LOCAL_TZ (America/New_York) everywhere a "today" is computed — the server
    container runs UTC, and a naive date.today() there flips to tomorrow at
    8pm ET, which would compose tomorrow's orders tonight.
  * Every external read is fail-soft: a missing tracker, an unreachable
    Supabase, or a school_data still mid-build degrades ONE pillar to empty,
    never takes compose() down.
  * Read-only toward the vault CSVs, no send capability anywhere, and it never
    drafts submittable schoolwork — all four courses ban AI on submissions, so
    school orders point at work, they don't do it.
  * Persistence goes through intake._load_state/_save_state ONLY. Those write
    json.dumps and _load_state's ilike filter depends on that exact byte
    layout — hand-serializing here would make our keys invisible to reads.

Wired by init() from app.py — imports its sibling readers directly, no hooks.
"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import intake
import august_tracker
import ad_creative_pipeline
import training_sync
import training_schedule

# Alex's day, not the container's — same constant, same reason as app.py /
# training_sync.py / school_data.py.
LOCAL_TZ = ZoneInfo("America/New_York")

# Injected by init()
supabase = None

# State keys (rows live in Supabase via intake's state helpers).
SCORECARD_KEY = "orders:scorecard"   # {"days": {iso_date: {"note", "pillars"}}}
SENDS_KEY = "orders:sends"           # {"sends": {brand: [iso_date, ...]}}

PILLARS = ("school", "ball", "money", "sleep")
MAX_ORDERS = 8
SCORECARD_KEEP_DAYS = 60             # bound the state row; streaks never need more

# Rank tiers — lower composes earlier. The whole point of the list is that a
# problem set due tonight outranks a workout, which outranks reading ahead.
_T_DEADLINE = 0   # due today / overdue — points or money on the line NOW
_T_EXAM = 1       # exam runway <= 10 days out
_T_MONEY = 2      # follow-ups due, warmup streak, send-log gaps
_T_TRAINING = 3   # today's card, practice, calendar big stuff
_T_PREP = 4       # before-next-class reading, quiz pointers, future dues
_T_HYGIENE = 5    # 50/50 logging, sleep guard

# Obligation routing: practice/lift/meeting text belongs to ball; school-shaped
# text is already owned by the study plan (double-listing it would burn slots).
_BALL_WORDS = ("practice", "lift", "meeting", "game", "scrimmage", "film")
_SCHOOL_WORDS = ("exam", "midterm", "final", "quiz", "class", "lecture",
                 "homework", "problem set")


def init(supabase_client):
    global supabase
    supabase = supabase_client


# ---------------------------------------------------------------------------
# time — every "today" flows through here
# ---------------------------------------------------------------------------

def _now(now=None) -> datetime:
    """An aware LOCAL_TZ datetime. Naive input is taken as already-local;
    aware input is converted, so a UTC caller still gets Alex's date."""
    if now is None:
        return datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=LOCAL_TZ)
    return now.astimezone(LOCAL_TZ)


def _parse_iso_date(s):
    try:
        return date.fromisoformat(str(s or "").strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# state — intake's helpers only (see module docstring for why)
# ---------------------------------------------------------------------------

def _load(key: str) -> dict:
    try:
        return intake._load_state(key)
    except Exception:
        return {"key": key}


def _save(state: dict) -> bool:
    try:
        intake._save_state(state)
        return True
    except Exception:
        return False


def _scorecard_days() -> dict:
    days = _load(SCORECARD_KEY).get("days")
    return days if isinstance(days, dict) else {}


def _sends() -> dict:
    sends = _load(SENDS_KEY).get("sends")
    return sends if isinstance(sends, dict) else {}


# ---------------------------------------------------------------------------
# pillar readers — each returns [] / {} on any failure
# ---------------------------------------------------------------------------

def _study_plan(today: date) -> dict:
    """school_data.study_plan_data, or {} while it doesn't exist / can't run.
    Imported lazily: the function is being built against the same contract in a
    parallel session, and this module must not care whether it landed yet."""
    try:
        import school_data
        fn = getattr(school_data, "study_plan_data", None)
        if fn is None:
            return {}
        return fn(for_date=today.isoformat()) or {}
    except Exception:
        return {}


def _item_text(it) -> str:
    if isinstance(it, str):
        return it.strip()
    if isinstance(it, dict):
        for k in ("title", "name", "text", "task"):
            if isinstance(it.get(k), str) and it[k].strip():
                return it[k].strip()
    return str(it)


_DUE_TAIL_RE = re.compile(
    r"\s*[—-]\s*due\s+(?:[A-Za-z]{3},?\s+)?([A-Z][a-z]{2})\s+(\d{1,2})\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _item_due(it, anchor: date = None):
    """Due date of a study-plan item. school_data emits STRINGS shaped
    "<title> (<type>) — due Thu Aug 27"; the month name carries no year, so we
    anchor to the plan date and wrap forward across New Year (a "due Jan 5"
    seen in December is next year, not eleven months late)."""
    if isinstance(it, dict):
        return _parse_iso_date(it.get("due") or it.get("due_date"))
    if isinstance(it, str) and anchor is not None:
        m = _DUE_TAIL_RE.search(it)
        if m and m.group(1) in _MONTHS:
            try:
                d = date(anchor.year, _MONTHS[m.group(1)], int(m.group(2)))
            except ValueError:
                return None
            if (d - anchor).days < -180:
                d = d.replace(year=anchor.year + 1)
            return d
    return None


def _item_points(it):
    if isinstance(it, dict):
        p = it.get("points")
        return p if isinstance(p, (int, float)) and p else None
    return None


def _school_orders(today: date) -> list:
    """(tier, order) pairs from the study plan. Overdue/due-today carry the
    points at stake; everything else is prep with its date."""
    plan = _study_plan(today)
    out = []

    # Spaced-review queue. Without this the ledger was write-only: reviews came
    # due and no proactive surface ever said so, so "3 recalls before the exam"
    # depended on Alex remembering to ask.
    try:
        import school_data
        for line in school_data.reviews_due(limit=3):
            out.append((_T_PREP, _order("school", f"recall: {line}",
                       "spaced review keeps it from re-learning cost", "today")))
    except Exception:
        pass

    per_course = plan.get("per_course") or {}
    for code in sorted(per_course):
        info = per_course[code] or {}
        next_class = info.get("next_class")
        for it in info.get("due_soon") or []:
            text, due, pts = _item_text(it), _item_due(it, today), _item_points(it)
            if isinstance(it, str) and due:
                text = _DUE_TAIL_RE.sub("", text).strip()
            if not text:
                continue
            pts_s = f" · {pts:g} pts" if pts else ""
            if due and due < today:
                out.append((_T_DEADLINE, _order("school", f"{code}: {text}",
                           f"was due {due.isoformat()}{pts_s}", "today")))
            elif due == today:
                out.append((_T_DEADLINE, _order("school", f"{code}: {text}",
                           f"due today{pts_s}", "today")))
            else:
                when = f"due {due.isoformat()}" if due else "due soon"
                out.append((_T_PREP, _order("school", f"{code}: {text}",
                           when + pts_s, "tonight")))
        for it in info.get("before_next_class") or []:
            text = _item_text(it)
            if not text:
                continue
            why = (f"next class {str(next_class)[:10]}" if next_class
                   else "before next class")
            out.append((_T_PREP, _order("school", f"{code}: {text}", why, "tonight")))
        ptr = info.get("quiz_pointer")
        if isinstance(ptr, str) and ptr.strip():
            # A quiz being GRADED TODAY is not "prep" — it ranks with the day's
            # other graded work, or the 3-4 line brief cap buries it.
            if info.get("quiz_today"):
                out.append((_T_EXAM, _order("school", f"{code}: {ptr.strip()}",
                           "graded today, no make-ups", "before class")))
            else:
                out.append((_T_PREP, _order("school", f"{code}: {ptr.strip()}",
                           "flagged in the syllabus", "tonight")))
    for ex in plan.get("exams") or []:
        if not isinstance(ex, dict):
            continue
        days_out = ex.get("days_out")
        if days_out is None:
            d = _parse_iso_date(ex.get("date"))
            days_out = (d - today).days if d else None
        if days_out is None or not (0 <= days_out <= 10):
            continue
        out.append((_T_EXAM, _order(
            "school",
            f"{ex.get('course', '?')} {ex.get('name', 'exam')} — start/continue runway",
            f"exam {ex.get('date')}, {days_out}d out", "daily")))
    return out


def _csv_rows() -> list:
    """Prospect tracker rows, read-only, [] whenever this node has no tracker."""
    try:
        import csv
        path = ad_creative_pipeline._tracker_path()
        if not path:
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _followups_due(today: date) -> dict:
    """{"due": [{brand, which, sent, age}], "unlogged": [brand]}.

    Union of three views of the same clock:
      1. ad_creative_pipeline.due_followups() — the CSV as the pipeline reads it.
         Its elif emits only ONE stage per row, so a 9-day-old send with neither
         follow-up logged reports just "+3d" and silently hides that "+7d" is
         also overdue.
      2. Our own CSV rescan with independent ifs — the elif-undercount fix.
         The same 9-day-old row here yields BOTH "+3d" and "+7d", because both
         really are due; deduped against (1) by (brand, stage).
      3. The chat-logged sends state ("orders:sends"). No follow-up columns
         exist there, so age alone drives both stages — and any brand logged
         here whose CSV row has no sent_date lands in "unlogged", because a
         send the tracker can't see is a follow-up clock that never starts.
    """
    due, seen = [], set()

    def add(brand, which, sent, age):
        k = (brand, which)
        if brand and k not in seen:
            seen.add(k)
            due.append({"brand": brand, "which": which, "sent": sent, "age": age})

    try:
        anchor = datetime.combine(today, time(12, 0), tzinfo=LOCAL_TZ)
        for d in ad_creative_pipeline.due_followups(anchor):
            sent_d = _parse_iso_date(d.get("sent"))
            age = (today - sent_d).days if sent_d else 0
            add((d.get("brand") or "").strip(), d.get("which"), d.get("sent"), age)
    except Exception:
        pass

    csv_by_brand = {}
    for row in _csv_rows():
        brand = (row.get("brand") or "").strip()
        if not brand:
            continue
        csv_by_brand[brand] = row
        if (row.get("replied") or "").strip() or (row.get("outcome") or "").strip():
            continue
        sent_d = _parse_iso_date(row.get("sent_date"))
        if not sent_d:
            continue
        age = (today - sent_d).days
        if age >= 3 and not (row.get("followup1_date") or "").strip():
            add(brand, "+3d", sent_d.isoformat(), age)
        if age >= 7 and not (row.get("followup2_date") or "").strip():
            add(brand, "+7d", sent_d.isoformat(), age)

    unlogged = set()
    for brand, dates in _sends().items():
        row = csv_by_brand.get(brand)
        if row is not None and ((row.get("replied") or "").strip()
                                or (row.get("outcome") or "").strip()):
            continue
        if row is None or not (row.get("sent_date") or "").strip():
            unlogged.add(brand)
        parsed = sorted(d for d in (_parse_iso_date(x) for x in dates or []) if d)
        if not parsed:
            continue
        latest = parsed[-1]
        age = (today - latest).days
        f1 = row is not None and (row.get("followup1_date") or "").strip()
        f2 = row is not None and (row.get("followup2_date") or "").strip()
        if age >= 3 and not f1:
            add(brand, "+3d", latest.isoformat(), age)
        if age >= 7 and not f2:
            add(brand, "+7d", latest.isoformat(), age)
    return {"due": due, "unlogged": sorted(unlogged)}


def _money_orders(today: date) -> list:
    out = []
    # August tracker: overdue / due-today steps are hard deadlines; daily streak
    # steps (warmup sends) belong with the rest of the money block.
    try:
        st = august_tracker.status()
    except Exception:
        st = {}
    for s in st.get("overdue") or []:
        if s.get("owner") == "alex":
            out.append((_T_DEADLINE, _order("money", s["title"],
                       f"was due {s['due']}", "today")))
    overdue_ids = {s["id"] for s in st.get("overdue") or []}
    for s in st.get("due_today") or []:
        if s.get("owner") == "alex" and s["id"] not in overdue_ids:
            out.append((_T_DEADLINE, _order("money", s["title"], "due today", "today")))
    for s in st.get("ready") or []:
        if s.get("daily") and s.get("owner") == "alex":
            out.append((_T_MONEY, _order("money", s["title"],
                       "miss a day and the run restarts", "today")))
    fu = _followups_due(today)
    for d in fu["due"]:
        out.append((_T_MONEY, _order("money", f"{d['which']} follow-up — {d['brand']}",
                   f"sent {d['sent']}, {d['age']}d ago", "today")))
    if fu["unlogged"]:
        names = ", ".join(fu["unlogged"][:4])
        out.append((_T_MONEY, _order("money", "Log your sends in the prospect tracker",
                   f"{names}: sent per chat, no sent_date in the CSV", "today")))
    return out


# Bullets that appear on EVERY workout card — they carry no information about
# what today's session actually is, so a headline built from them reads the same
# seven days a week (the bug: every card starts "Warmup / 50/50").
_GENERIC_BULLETS = ("warmup", "50/50", "mobility", "cars", "crawls")


def _card_headline(card: str) -> str:
    """The DIFFERENTIATING work on a workout card, as a one-line order title.

    Skips the bullets common to every card so the title names what makes today
    today (Good Finishing, which bag, which lift). Falls back to the first
    bullets if a card is nothing but the common ones."""
    bullets = [ln.strip().lstrip("-•*").strip()
               for ln in (card or "").splitlines() if ln.strip()]
    distinct = [b for b in bullets
                if b.strip().lower() not in _GENERIC_BULLETS]
    return " · ".join((distinct or bullets)[:3])


_GYM_TITLE = ("gym", "50/50", "basketball", "practice", "lift", "shoot")
# "Sleep 9:45 → wake" — the clock inside a block label, which beats the 30-min
# slot the cell happens to sit in.
_LABEL_CLOCK_RE = re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)


def _training_windows(p: dict, today: date) -> str:
    """Today's real gym/ball clock ranges from the grid, e.g. '7:25–8:30a + 2:30–5p'.

    Alex dictates when he trains and it differs by day, so the window has to be
    read from the grid — a hardcoded window is wrong the moment he moves a
    session, and it was ("4-6 PM" survived a full rebuild of the week)."""
    try:
        evs = [e for e in training_schedule.events_for_date(p, today)
               if e["start"].date() == today
               and any(w in (e.get("title") or "").lower() for w in _GYM_TITLE)]
    except Exception:
        return ""
    spans, seen = [], set()
    for e in evs:
        span = f"{training_schedule.clock(e['start'])}–{training_schedule.clock(e['end'])}"
        if span not in seen:
            seen.add(span)
            spans.append(span)
    return " + ".join(spans[:3])


def _training_orders(now: datetime, today: date) -> list:
    out = []
    try:
        p = training_sync.parsed()
    except Exception:
        p = None
    if not p:
        return out

    card = (p.get("workouts") or {}).get(training_schedule.day_index(today), "")
    windows = _training_windows(p, today)
    if card:
        head = _card_headline(card)
        out.append((_T_TRAINING, _order("ball",
                   f"{windows}: {head}" if windows else head,
                   "written on today's card", windows or "today")))

    try:
        obligations = training_schedule.obligations_for_date(p, today)
    except Exception:
        obligations = []
    # Route each LINE, not each entry. One calendar day holds a 1-3 line list
    # (set_big_obligation stores the whole day's list as one text), so judging
    # the joined string meant a single school word swallowed everything else on
    # that day: Monday's "First day of classes …\nBlood test (Quest) 2:40 PM"
    # dropped a medical appointment from every surface because "class" matched.
    for text in obligations:
        for line in str(text).splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if any(w in low for w in _SCHOOL_WORDS):
                continue  # the study plan owns school; don't double-list
            if any(w in low for w in _BALL_WORDS):
                out.append((_T_TRAINING, _order("ball", line,
                           "on today's schedule", "today")))
            else:
                out.append((_T_TRAINING, _order("life", line,
                           "on the calendar for today", "today")))

    if card:
        out.append((_T_HYGIENE, _order("ball", "Log 50/50 numbers after the session",
                   "no numbers, no trend line", "post-workout")))

    # Sleep guard: an early first block tomorrow makes tonight's bedtime a
    # decision with a deadline, so it gets an order, not a vibe.
    tomorrow = today + timedelta(days=1)
    try:
        evs = [e for e in training_schedule.events_for_date(p, tomorrow)
               if e["start"].date() == tomorrow]
    except Exception:
        evs = []
    if evs and evs[0]["start"].time() <= time(9, 30):
        t = training_schedule.clock(evs[0]["start"])
        # Lights-out comes from tonight's own Sleep block — Alex dictated 9:45 on
        # some nights and 10:00 on others. A hardcoded hour coached him an hour
        # past his own plan every school night; sleep is a first-order recovery
        # lever for the season, so the guard has to agree with the grid.
        lights = ""
        try:
            for e in training_schedule.events_for_date(p, today):
                title = (e.get("title") or "").strip()
                if not title.lower().startswith("sleep") \
                        or e["start"].date() != today \
                        or e["start"].time() < time(18, 0):
                    continue
                # The LABEL beats the slot: the grid is 30-minute cells, so a
                # 9:45 bedtime is stored in the 10:00 cell and titled
                # "Sleep 9:45 → wake". The label is what Alex actually said.
                m = _LABEL_CLOCK_RE.search(title)
                mins = training_sync.parse_clock(m.group(1)) if m else None
                if mins is None:
                    lights = training_schedule.clock(e["start"])
                else:
                    if mins < 12 * 60:        # a bare evening hour means PM
                        mins += 12 * 60
                    lights = training_schedule.clock(
                        datetime.combine(today, time(mins // 60 % 24, mins % 60)))
                break
        except Exception:
            lights = ""
        if not lights:
            # No Sleep block (a blank weekend column). 8.5h before the first
            # block is the LATEST defensible bedtime, not a recommendation —
            # 8.5h before a 9am start is 12:30am — so cap it at 11pm and never
            # coach a later night than the old constant did.
            latest = datetime.combine(tomorrow, evs[0]["start"].time()) - timedelta(hours=8.5)
            cap = datetime.combine(today, time(23, 0))
            lights = training_schedule.clock(min(latest, cap))
        out.append((_T_HYGIENE, _order("life", f"Lights out {lights} — first block {t}",
                   f"tomorrow starts at {t}", "tonight")))
    return out


def _order(pillar: str, title: str, why: str, when: str) -> dict:
    return {"pillar": pillar, "title": title, "why": why, "when": when}


# ---------------------------------------------------------------------------
# scorecard + streaks
# ---------------------------------------------------------------------------

def _streaks(days: dict = None) -> dict:
    """Consecutive days each pillar was logged TRUE, counting back from the most
    recent logged day. A missing day breaks the streak — an unlogged day is an
    unearned day, and streaks that survive gaps aren't streaks."""
    days = _scorecard_days() if days is None else days
    out = {p: 0 for p in PILLARS}
    dates = sorted((d for d in (_parse_iso_date(k) for k in days) if d), reverse=True)
    if not dates:
        return out
    for p in PILLARS:
        expect = dates[0]
        n = 0
        while True:
            entry = days.get(expect.isoformat())
            pillars = (entry or {}).get("pillars") or {}
            if not pillars.get(p):
                break
            n += 1
            expect -= timedelta(days=1)
        out[p] = n
    return out


def log_scorecard(note: str, pillars: dict | None = None) -> str:
    today = _now().date().isoformat()
    state = _load(SCORECARD_KEY)
    days = state.get("days")
    if not isinstance(days, dict):
        days = {}
    days[today] = {
        "note": (note or "").strip(),
        "pillars": {p: bool((pillars or {}).get(p)) for p in PILLARS} if pillars else {},
    }
    # Bound the row — keep the newest window only.
    keep = sorted(days)[-SCORECARD_KEEP_DAYS:]
    state["days"] = {k: days[k] for k in keep}
    saved = _save(state)
    streaks = _streaks(state["days"])
    line = " · ".join(f"{p} {n}d" for p, n in streaks.items())
    msg = f"Scorecard logged for {today}. Streaks: {line}."
    if not saved:
        msg += " (Warning: couldn't persist to Supabase — may not survive a restart.)"
    return msg


def scorecard_text(days: int = 7) -> str:
    logged = _scorecard_days()
    if not logged:
        return ("No scorecard entries yet. Log tonight with log_scorecard — "
                "one sentence plus school/ball/money/sleep is enough.")
    lines = [f"**Scorecard — last {days} days**"]
    for k in sorted(logged, reverse=True)[:days]:
        entry = logged[k] or {}
        pillars = entry.get("pillars") or {}
        marks = "  ".join(
            f"{p} " + ("✓" if pillars.get(p) else "✗" if p in pillars else "–")
            for p in PILLARS)
        note = (entry.get("note") or "").strip()
        lines.append(f"{k}  {marks}" + (f"  — {note}" if note else ""))
    streaks = _streaks(logged)
    lines.append("Streaks: " + " · ".join(f"{p} {n}d" for p, n in streaks.items()))
    return "\n".join(lines)


def log_outreach_send(brand: str, when: str = "") -> str:
    brand = (brand or "").strip()
    if not brand:
        return "Which brand? Give me the name as it appears in the tracker."
    d = _parse_iso_date(when) if when.strip() else _now().date()
    if d is None:
        return f"Couldn't read '{when}' as a date — use YYYY-MM-DD."
    state = _load(SENDS_KEY)
    sends = state.get("sends")
    if not isinstance(sends, dict):
        sends = {}
    dates = sends.setdefault(brand, [])
    if d.isoformat() not in dates:
        dates.append(d.isoformat())
        dates.sort()
    state["sends"] = sends
    saved = _save(state)
    msg = (f"Logged: {brand} sent {d.isoformat()}. Follow-ups come due "
           f"{(d + timedelta(days=3)).isoformat()} (+3d) and "
           f"{(d + timedelta(days=7)).isoformat()} (+7d).")
    # Mirror into the tracker CSV ourselves. Asking Alex to double-enter a send
    # he just reported — and then spending a daily order nagging him when the
    # two clocks disagreed — was clerical work the system created for itself.
    try:
        mirror = ad_creative_pipeline.reconcile_prospect_csv(sends=sends)
        if mirror.startswith("tracker updated"):
            msg += " Tracker CSV updated to match."
        elif mirror.startswith("skipped"):
            msg += " (Tracker lives on the Mac — it'll sync there.)"
    except Exception:
        pass
    if not saved:
        msg += " (Warning: couldn't persist to Supabase.)"
    return msg


# ---------------------------------------------------------------------------
# the composer
# ---------------------------------------------------------------------------

def compose(now=None) -> dict:
    """The ranked day: {"date", "orders": [...], "streaks": {pillar: int}}.

    Tier order is the contract: hard deadlines today, then exam runways, then
    money follow-ups/streaks, then training, then prep, then hygiene — capped
    at MAX_ORDERS because a 20-item list is a list, not orders."""
    now = _now(now)
    today = now.date()
    tiered = []
    tiered += _school_orders(today)
    tiered += _money_orders(today)
    tiered += _training_orders(now, today)
    tiered.sort(key=lambda t: t[0])  # stable — insertion order holds within a tier
    picked = tiered[:MAX_ORDERS]
    # Basketball is a goal, not filler: on a training day the cap must never
    # squeeze every ball order out behind money/school backlog. If the cut list
    # has none, the top ball order takes the last slot.
    if not any(o["pillar"] == "ball" for _t, o in picked):
        ball = next(((t, o) for t, o in tiered if o["pillar"] == "ball"), None)
        if ball is not None:
            picked = picked[:-1] + [ball] if len(picked) >= MAX_ORDERS else picked + [ball]
    orders = []
    for i, (_tier, o) in enumerate(picked, 1):
        orders.append({"rank": i, **o})
    return {"date": today.isoformat(), "orders": orders, "streaks": _streaks()}


def orders_text(day: str = "") -> str:
    """Human rendering of compose() for '', 'today', 'tomorrow' or an ISO date."""
    now = _now()
    d = (day or "").strip().lower()
    if d in ("", "today"):
        pass
    elif d == "tomorrow":
        now += timedelta(days=1)
    else:
        target = _parse_iso_date(d)
        if target is None:
            return f"Couldn't read '{day}' — use today, tomorrow, or YYYY-MM-DD."
        now = datetime.combine(target, time(12, 0), tzinfo=LOCAL_TZ)
    res = compose(now)
    lines = [f"**Daily orders — {res['date']}**"]
    if not res["orders"]:
        lines.append("Nothing composed — every source is quiet or unreachable.")
    for o in res["orders"]:
        lines.append(f"{o['rank']}. [{o['pillar']}] {o['title']} — {o['why']} (do: {o['when']})")
    lines.append("Streaks: " + " · ".join(f"{p} {n}d" for p, n in res["streaks"].items()))
    return "\n".join(lines)


def brief_lines(now=None, limit: int = 4) -> list:
    """Top orders as short lines for the morning brief: 'N) title — why', ≤90 chars."""
    out = []
    for o in compose(now)["orders"][:limit]:
        prefix, why = f"{o['rank']}) ", f" — {o['why']}"
        # Trim the TITLE, never the why — the why is the half that gets it done.
        title = o["title"]
        budget = 90 - len(prefix) - len(why)
        if len(title) > budget:
            title = title[:max(budget - 1, 0)] + "…"
        out.append((prefix + title + why)[:90])
    return out


def evening_lines(now=None) -> list:
    """The nightly nudge: ask for the scorecard, capture the day's real data,
    preview tomorrow's top 2.

    The capture asks ride the conversation that already happens every night —
    the lesson from two empty ledgers is that a logging tool nobody is prompted
    to use stays header-only forever."""
    lines = ["Scorecard time: how did school/ball/money/sleep go? "
             "(one sentence is enough)"]
    ref = _now(now)
    today = ref.date()

    # 50/50 numbers, only if today's grid actually had a shooting block and
    # nothing is logged for today yet.
    try:
        p = training_sync.parsed()
        had_5050 = any("50/50" in (e.get("title") or "")
                       for e in training_schedule.events_for_date(p, today)
                       if e["start"].date() == today)
        if had_5050 and not training_sync.logged_5050_on(ref):
            lines.append("50/50 numbers? Say: log 50/50: <threes> and <free throws>.")
    except Exception:
        pass

    # What actually got reviewed today — turns the nightly check-in into the
    # spaced-repetition ledger's write path.
    try:
        import school_data
        met = school_data.courses_meeting_on(today)
        if met:
            lines.append(f"Which of these did you actually study today — {', '.join(met)}? "
                         "Name them and I'll log the recalls.")
    except Exception:
        pass

    for o in compose(ref + timedelta(days=1))["orders"][:2]:
        lines.append(f"Tomorrow: {o['title']} — {o['why']}")
    return lines


# ---------------------------------------------------------------------------
# tool surface — wired into app.py's TOOLS via init-time extend
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_daily_orders",
        "description": (
            "Today's ranked orders across school, ball, money and life — deadlines "
            "first, capped at 8, each with a concrete why. Use when Alex asks what "
            "to do today, what matters, or for his daily plan. Optional day: "
            "'today', 'tomorrow', or YYYY-MM-DD."),
        "input_schema": {
            "type": "object",
            "properties": {"day": {"type": "string",
                                   "description": "'today', 'tomorrow', or YYYY-MM-DD."}},
        },
    },
    {
        "name": "log_scorecard",
        "description": (
            "Log tonight's scorecard: one sentence plus school/ball/money/sleep as "
            "booleans. Use when Alex reports how the day went. Streaks are computed "
            "from these logs."),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "One-sentence day summary."},
                "pillars": {
                    "type": "object",
                    "description": "Booleans for school, ball, money, sleep.",
                    "properties": {p: {"type": "boolean"} for p in PILLARS},
                },
            },
            "required": ["note"],
        },
    },
    {
        "name": "get_scorecard",
        "description": ("The last week of scorecard logs as ✓/✗ per pillar plus "
                        "current streaks. Use when Alex asks how he's been doing."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_outreach_send",
        "description": (
            "Record that Alex sent an outreach email to a brand (he sends; CLARVIS "
            "never does). Starts the +3d/+7d follow-up clocks. Optional when: "
            "YYYY-MM-DD, default today."),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string", "description": "Brand name as in the tracker."},
                "when": {"type": "string", "description": "Send date YYYY-MM-DD (default today)."},
            },
            "required": ["brand"],
        },
    },
]

TOOL_NAMES = tuple(t["name"] for t in TOOL_SCHEMAS)

TOOL_STATUS_LABELS = {
    "get_daily_orders": "Composing today's orders…",
    "log_scorecard": "Logging the scorecard…",
    "get_scorecard": "Pulling up the scorecard…",
    "log_outreach_send": "Starting the follow-up clock…",
}


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_daily_orders":
        return orders_text(tool_input.get("day", ""))
    if tool_name == "log_scorecard":
        return log_scorecard(tool_input.get("note", ""), tool_input.get("pillars"))
    if tool_name == "get_scorecard":
        return scorecard_text()
    if tool_name == "log_outreach_send":
        return log_outreach_send(tool_input.get("brand", ""),
                                 tool_input.get("when", ""))
    return f"Unknown tool: {tool_name}"
