"""
do_actions.py — what a notification link actually SHOWS and DOES.

`action_links.py` proves a link is genuine; this decides what's behind it. One
module rather than more routes in the 7k-line app.py, and because the interesting
part is content, not HTTP: a nudge that says "handle the Emily thing" is a nudge
that gets postponed, so every /do page has to answer three questions in the order
a person actually asks them —

    WHAT is this            (the item, in its own words)
    WHY now                 (due when, from whom, what breaks if it slips)
    WHAT DO I PHYSICALLY DO (numbered steps, and a button that starts step 1)

The steps are DETERMINISTIC, derived from the item's kind. They are not written by
a model on a polling loop (see CLAUDE.md: don't put a reasoning agent on a poll,
and don't make a capability depend on the model electing to use it) — an emailed
draft always has the same four steps, and a step list that's always there beats a
better-worded one that's there 70% of the time.

`perform()` is the write half: the one-tap ops a notification button or the page's
own buttons can fire. Every op is narrow, reversible-in-spirit (done/snooze/drop),
and gated by the token's own op list — the token minted for "mark done" cannot be
replayed as "drop it". Nothing here can send an email, move money, or delete: the
consequential-action queue keeps its own gate, and this layer only ever RECORDS a
decision Alex already made with his thumb.
"""

from datetime import datetime

import action_links as al

# Injected by app.py at boot (same shape as every other subsystem here).
supabase = None
intake_mod = None
task_tracker = None
outbox_mod = None
resolve_pending = None      # app.resolve_pending_action(row_id, decision)
LOCAL_TZ = None


def init(supabase_client=None, intake_module=None, tracker=None, outbox=None,
         resolve_pending_fn=None, local_tz=None):
    global supabase, intake_mod, task_tracker, outbox_mod, resolve_pending, LOCAL_TZ
    supabase = supabase_client
    intake_mod = intake_module
    task_tracker = tracker
    outbox_mod = outbox
    resolve_pending = resolve_pending_fn
    LOCAL_TZ = local_tz


def _now():
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()


def _fmt_due(due: str) -> str:
    try:
        dt = datetime.fromisoformat(str(due))
    except (TypeError, ValueError):
        return str(due or "")
    if len(str(due).strip()) <= 10:
        return dt.strftime("%a %b %-d")
    return dt.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


# ============================================================
# READ — what the page shows
# ============================================================

def resolve(payload: dict) -> dict:
    """Token payload -> everything the page needs.

    Always returns a dict; an item that has vanished (deleted task, dismissed
    intake) comes back with `gone=True` rather than an error, because "this is
    already handled" is a legitimate and reassuring answer to a tap on a
    three-day-old notification."""
    kind, ref = payload.get("k"), payload.get("r", "")
    view = {"kind": kind, "ref": ref, "title": "", "why": "", "detail": "",
            "steps": [], "link": "", "link_label": "", "ops": list(payload.get("o") or []),
            "gone": False, "done_label": "Mark it done", "form": []}
    try:
        if kind == al.KIND_OUTBOX:
            _resolve_outbox(view, ref)
        elif kind == al.KIND_OUTBOX_ALL:
            _resolve_outbox_all(view)
        elif kind == al.KIND_INTAKE:
            _resolve_intake(view, ref)
        elif kind == al.KIND_TASK:
            _resolve_task(view, ref)
        elif kind == al.KIND_APPROVAL:
            _resolve_approval(view, ref)
        elif kind == al.KIND_STEP:
            _resolve_step(view, ref)
        elif kind == al.KIND_SCORECARD:
            _resolve_scorecard(view, ref)
        elif kind == al.KIND_PACE:
            _resolve_pace(view, ref)
    except Exception as e:
        print(f"do_actions: resolve failed ({e})")
        view["title"] = view["title"] or payload.get("l") or "Open CLARVIS"
        view["why"] = "Couldn't load the live details — open CLARVIS for the full picture."
    if not view["title"]:
        view["title"] = payload.get("l") or "Nothing left to do here"
        view["gone"] = True
    return view


def _resolve_outbox(view, ref):
    item = outbox_mod.get(int(ref)) if outbox_mod else None
    if not item or item.get("status") != outbox_mod.OPEN:
        view["gone"] = True
        view["title"] = "Already handled"
        view["why"] = "This one's closed — nothing waiting on you here."
        return
    view["title"] = item.get("title", "")
    view["detail"] = item.get("detail", "")
    view["steps"] = item.get("steps") or []
    view["link"] = item.get("link", "")
    view["link_label"] = ("Open Gmail Drafts" if item.get("kind") == "email_draft"
                          else "Open it")
    view["done_label"] = "Sent it" if item.get("kind") == "email_draft" else "Done"
    line = outbox_mod.summary_line(item)
    age = line.split(" — ", 1)[1] if " — " in line else ""
    view["why"] = ("Ready and " + age if age
                   else "Ready — it just needs the part only you can do.")


def _resolve_outbox_all(view):
    items = outbox_mod.open_items() if outbox_mod else []
    if not items:
        view["gone"] = True
        view["title"] = "Nothing waiting"
        view["why"] = "Your outbox is clear."
        return
    view["title"] = f"{len(items)} thing(s) waiting on you"
    view["why"] = "Each one is finished except for the part only you can do."
    view["children"] = [{
        "id": it["id"], "title": it.get("title", ""),
        "line": outbox_mod.summary_line(it),
        "link": it.get("link", ""),
        "url": al.url(al.KIND_OUTBOX, str(it["id"]), ops=("done", "snooze", "drop")),
    } for it in items]
    view["steps"] = ["Open each one below.",
                     "Do the last step yourself — send, sign, pay, confirm.",
                     "Tap done so it stops surfacing."]


def _resolve_intake(view, ref):
    ev = intake_mod._event_row(int(ref)) if intake_mod else None
    if not ev or ev.get("status") not in ("new", "accepted"):
        view["gone"] = True
        view["title"] = "Already triaged"
        view["why"] = "This one is closed."
        return
    items = ev.get("items") or []
    first = items[0] if items else {}
    view["title"] = first.get("text") or ev.get("preview", "")[:120]
    due = first.get("due")
    bits = []
    if due:
        bits.append(f"Due {_fmt_due(due)}")
    sender = ev.get("sender") or "?"
    bits.append(f"from {sender} via {ev.get('source', '?')}")
    view["why"] = " · ".join(bits)
    view["detail"] = (ev.get("preview") or "").strip()[:1200]
    view["steps"] = ["Read the original above — it's the whole ask.",
                     "Do it now if it's under two minutes.",
                     "If it needs a real block, tap Snooze and CLARVIS will "
                     "raise it again later today.",
                     "If it stopped mattering, tap Not doing it — that's a "
                     "real answer and it stops the reminders."]


def _resolve_task(view, ref):
    t = task_tracker.get(int(ref)) if task_tracker else None
    if not t or t.get("status") in ("done", "dropped"):
        view["gone"] = True
        view["title"] = "Already closed"
        view["why"] = "This task is done or dropped."
        return
    view["title"] = t.get("title", "")
    view["detail"] = (t.get("description") or "").strip()[:1200]
    due = t.get("due")
    view["why"] = (f"Due {_fmt_due(due)}" if due else "Open task") + \
                  f" · task #{t.get('id')}"
    view["steps"] = ["Start it now — the first two minutes are the whole fight.",
                     "Tap Done the moment it's finished.",
                     "Snooze if today genuinely isn't the day."]


def _resolve_approval(view, ref):
    if not supabase:
        return
    res = supabase.table("Agent Outputs").select("*").eq("id", int(ref)).execute()
    row = (res.data or [None])[0]
    if not row or row.get("agent_name") != "jarvis_pending_action":
        view["gone"] = True
        view["title"] = "Not found"
        return
    import json
    action = json.loads(row["output_text"])
    if action.get("status") != "pending":
        view["gone"] = True
        view["title"] = f"Already {action.get('status')}"
        view["why"] = "You decided this one already."
        return
    view["title"] = action.get("display", "(unknown action)")
    view["why"] = "CLARVIS is blocked on your decision — it will not act until you say."
    view["detail"] = str(action.get("action", ""))[:800]
    view["steps"] = ["Read what it wants to do.",
                     "Approve only if you'd do it yourself right now.",
                     "Deny is free — it can always ask again with a better plan."]


def _resolve_step(view, ref):
    try:
        import august_tracker
        st = august_tracker.status()
    except Exception:
        return
    for bucket in ("overdue", "ready", "blocked"):
        for s in st.get(bucket, []):
            if s.get("id") != ref:
                continue
            view["title"] = s.get("title", ref)
            why = ""
            try:
                why = august_tracker._why(s)
            except Exception:
                pass
            view["why"] = why or "Part of the money plan."
            do_line = ""
            try:
                do_line = august_tracker._do(s)
            except Exception:
                pass
            view["steps"] = [do_line] if do_line else [
                "Open the plan in the vault and do the next step."]
            return


# ============================================================
# WRITE — the one-tap ops
# ============================================================

def perform(payload: dict, op: str, form=None) -> dict:
    """Run one op. Returns {ok, message}. Refuses anything the token didn't grant."""
    if not al.allows(payload, op):
        return {"ok": False, "message": "That link doesn't allow this action."}
    kind, ref = payload.get("k"), payload.get("r", "")
    try:
        if kind in (al.KIND_OUTBOX, al.KIND_OUTBOX_ALL):
            return _do_outbox(ref, op)
        if kind == al.KIND_INTAKE:
            return _do_intake(ref, op)
        if kind == al.KIND_TASK:
            return _do_task(ref, op)
        if kind == al.KIND_APPROVAL:
            return _do_approval(ref, op)
        if kind == al.KIND_STEP:
            return _do_step(ref, op)
        if kind == al.KIND_SCORECARD:
            return _do_scorecard(ref, op, form or {})
        if kind == al.KIND_PACE:
            return _do_pace(ref, op, form or {})
    except Exception as e:
        print(f"do_actions: perform failed ({e})")
        return {"ok": False, "message": f"Couldn't do that: {str(e)[:120]}"}
    return {"ok": False, "message": "Nothing to do."}


def _do_outbox(ref, op):
    if not outbox_mod:
        return {"ok": False, "message": "Outbox unavailable."}
    item_id = int(ref)
    if op in ("done", "sent"):
        outbox_mod.close(item_id, outbox_mod.DONE, note="closed from notification")
        return {"ok": True, "message": "Logged — I'll stop asking about that one."}
    if op == "snooze":
        outbox_mod.snooze(item_id, hours=3)
        return {"ok": True, "message": "Snoozed 3 hours."}
    if op == "drop":
        outbox_mod.close(item_id, outbox_mod.DROPPED, note="dropped from notification")
        return {"ok": True, "message": "Dropped. It won't come back."}
    return {"ok": False, "message": "Unknown action."}


def _do_intake(ref, op):
    row_id = int(ref)
    if op == "done":
        return {"ok": True,
                "message": intake_mod.dismiss_intake(row_id, resolution="done by Alex")}
    if op == "drop":
        return {"ok": True,
                "message": intake_mod.dismiss_intake(row_id, resolution="not doing it")}
    if op == "snooze":
        # Intake rows have no snooze field of their own; accepting turns the item
        # into a real task, which is exactly the object a later reminder needs.
        return {"ok": True, "message": intake_mod.accept_intake(row_id)
                + " I'll raise it again as a task."}
    return {"ok": False, "message": "Unknown action."}


def _do_task(ref, op):
    task_id = int(ref)
    if op == "done":
        r = task_tracker.update_status(task_id, "done", "closed from notification")
        return {"ok": bool(r), "message": "Marked done." if r else "No such task."}
    if op == "drop":
        r = task_tracker.update_status(task_id, "dropped", "dropped from notification")
        return {"ok": bool(r), "message": "Dropped." if r else "No such task."}
    if op == "snooze":
        t = task_tracker.get(task_id)
        due = (t or {}).get("due") or ""
        try:
            from task_tracker import due_moment
            from datetime import timedelta
            moment = due_moment(due, tz=LOCAL_TZ) or _now()
            new_due = (max(moment, _now()) + timedelta(hours=3))
            task_tracker.set_due(task_id, new_due.strftime("%Y-%m-%dT%H:%M"))
        except Exception:
            pass
        return {"ok": True, "message": "Pushed three hours."}
    return {"ok": False, "message": "Unknown action."}


def _do_approval(ref, op):
    if not resolve_pending:
        return {"ok": False, "message": "Approval queue unavailable."}
    if op not in ("approve", "deny"):
        return {"ok": False, "message": "Unknown action."}
    res = resolve_pending(int(ref), op)
    return {"ok": bool(res.get("ok")),
            "message": (f"{op.title()}d." if res.get("ok")
                        else res.get("error", "Couldn't record that."))}


# ============================================================
# Money-plan steps — the Done button used to write NOTHING ("Noted — tick it in
# the vault"), so a step tapped done on 08-18 kept re-nudging as "N d LATE" for
# two weeks. mark_done records it in the shared state row (both nodes read it)
# and the Mac ticks the vault checkbox on its next maintenance tick.
# ============================================================

def _do_step(ref, op):
    if op != "done":
        return {"ok": False, "message": "Unknown action."}
    try:
        import august_tracker
        r = august_tracker.mark_done(str(ref))
    except Exception as e:
        return {"ok": False, "message": f"Couldn't record it: {str(e)[:120]}"}
    if not r.get("ok"):
        return {"ok": False, "message": r.get("error", "No such step.")}
    if r.get("already"):
        return {"ok": True, "message": f"Already banked: {r.get('step', ref)}."}
    tail = ""
    if r.get("unblocked"):
        tail = " Unblocked: " + "; ".join(r["unblocked"][:3]) + "."
    return {"ok": True, "message": f"Banked: {r.get('step', ref)}.{tail}"}


# ============================================================
# Capture pages — the two inputs the system genuinely cannot derive, reduced to
# taps. Every chat-only capture tool (scorecard, mark_prepared, reviews, grades)
# had zero entries after two weeks of classes: the return channel to CLARVIS is
# not chat, it is the notification shade and the page it opens.
# ============================================================

_PILLARS = (("school", "School — did the day's work get done?"),
            ("ball", "Ball — trained as planned?"),
            ("money", "Money — moved the outreach?"),
            ("sleep", "Sleep — lights out on time?"))


def _resolve_scorecard(view, ref):
    view["title"] = f"Scorecard — {ref}"
    view["done_label"] = "Log tonight"
    view["why"] = "Four taps. A streak only counts days that were logged."
    streak = ""
    try:
        import daily_orders
        s = daily_orders._streaks()
        streak = " · ".join(f"{p} {n}d" for p, n in s.items())
    except Exception:
        pass
    if streak:
        view["detail"] = f"Streaks: {streak}"
    view["form"] = [{"type": "toggle", "name": p, "label": label, "checked": False}
                    for p, label in _PILLARS]
    view["form"].append({"type": "text", "name": "note",
                         "label": "One line about today (optional)"})
    view["ops"] = [o for o in view.get("ops") or [] if o == "log"] or ["log"]


def _do_scorecard(ref, op, form):
    if op != "log":
        return {"ok": False, "message": "Unknown action."}
    try:
        import daily_orders
    except Exception as e:
        return {"ok": False, "message": f"Scorecard unavailable: {str(e)[:100]}"}
    pillars = {p: (str(form.get(p) or "").strip().lower() in ("1", "on", "true", "yes"))
               for p, _ in _PILLARS}
    note = str(form.get("note") or "").strip()[:240] or "logged from the phone"
    try:
        msg = daily_orders.log_scorecard(note, pillars)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't log it: {str(e)[:120]}"}
    hit = ", ".join(p for p, v in pillars.items() if v) or "nothing ticked"
    return {"ok": True, "message": f"Logged: {hit}. {msg}"[:300]}


_PACE_CHOICES = (("same", "caught up (through today)"),
                 ("+1", "+1 class ahead"),
                 ("+2", "+2 classes ahead"))


def _pace_courses():
    """[(code, next_dates[0:3])] for every prep course, from the class-day map."""
    import school_data
    from datetime import timedelta
    courses = [c for c in (school_data._load("courses.csv") or [])
               if school_data._lead_days(c) > 0]
    curriculum = school_data._load("curriculum.csv") or []
    cmap = school_data._class_day_map(courses, curriculum)
    today = _now().date()
    out = []
    for c in sorted(courses, key=lambda c: c.get("course") or ""):
        code = (c.get("course") or "").strip()
        nxt = [d for d in (cmap.get(code) or []) if d > today][:2]
        if len(nxt) < 2:   # no map (or term over): fall back to the calendar week
            nxt = [today + timedelta(days=2), today + timedelta(days=7)]
        out.append((code, nxt))
    return out


def _resolve_pace(view, ref):
    view["title"] = "Where did you get to this week?"
    view["done_label"] = "Set my pace"
    view["why"] = ("Per course: caught up means prepared through today; +1 / +2 "
                   "means you already worked the next one or two lectures.")
    try:
        rows = _pace_courses()
    except Exception as e:
        view["why"] = f"Couldn't load the courses ({str(e)[:80]})."
        return
    for code, nxt in rows:
        opts = []
        for value, label in _PACE_CHOICES:
            if value == "+1":
                label = f"+1 class (through {nxt[0].strftime('%a %b')} {nxt[0].day})"
            elif value == "+2":
                label = f"+2 classes (through {nxt[1].strftime('%a %b')} {nxt[1].day})"
            opts.append({"value": value, "label": label, "checked": value == "same"})
        view["form"].append({"type": "choice", "name": f"pace_{code}", "label": code,
                             "options": opts})
    view["ops"] = ["log"]


def _do_pace(ref, op, form):
    if op != "log":
        return {"ok": False, "message": "Unknown action."}
    try:
        import school_state
        rows = _pace_courses()
    except Exception as e:
        return {"ok": False, "message": f"Pace unavailable: {str(e)[:100]}"}
    today = _now().date()
    set_to = {}
    for code, nxt in rows:
        choice = str(form.get(f"pace_{code}") or "same").strip()
        d = {"same": today, "+1": nxt[0], "+2": nxt[1]}.get(choice, today)
        set_to[code] = d.isoformat()
    try:
        school_state.record_prepared(set_to)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't save it: {str(e)[:120]}"}
    shown = ", ".join(f"{c} → {d[5:]}" for c, d in set_to.items())
    return {"ok": True, "message": f"Pace set: {shown}. The Mac writes it to courses.csv "
                                   "on its next tick (≤30 min)."}
