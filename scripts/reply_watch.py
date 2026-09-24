#!/usr/bin/env python3
"""Splitframe reply watcher — the one thing that must never sit unseen.

Every run: read the studio inbox and spam for mail from any prospect domain or address in the
tracker, or from anyone answering on a thread we sent into,
and for each new one:
  - decide whether it is a HUMAN reply or an autoresponder (see auto_reply_reason)
  - human: nudge Alex's phone, stamp `replied` in `Money/prospect-tracker.csv` (backup first)
  - automatic: log it and leave `replied` empty, so the follow-up sequence stays alive
  - append to scripts/reply_watch.log either way
Read-only on Gmail. Never sends. Runs every 30 min under launchd (com.secondbrain.replywatch).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import socket
import sys
import time
from datetime import datetime

VAULT = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain")
TRACKER = os.path.join(VAULT, "Money", "prospect-tracker.csv")
STATE = os.path.expanduser("~/second-brain/scripts/reply_watch_state.json")
LOG = os.path.expanduser("~/second-brain/scripts/reply_watch.log")
OWN = {"splitframestudio.com", "gmail.com", "google.com", "hunter.io", "stripe.com", "icloud.com"}

# ---------------------------------------------------------------------------
# Watchdog.
#
# 2026-09-21: this job was "healthy" — loaded, live PID, exit status 0 — and had not run for 34
# hours. A run started Sunday 03:00 blocked on a network call during an overnight DNS wobble and
# never returned. launchd will not start a new instance while the previous one is still alive, so
# ONE hung run silently disables the job forever. That is strictly worse than the disabled plist
# found on 09-19, because every external signal says the job is fine.
#
# The Composio client takes no timeout argument, so a per-call timeout cannot cover this. The
# watchdog does: whatever the run is doing, it dies well inside the 30-minute interval, and launchd
# starts a clean one next tick. A missed scan costs 30 minutes; a hung scan costs every scan after
# it.
RUN_BUDGET_SECONDS = 600
socket.setdefaulttimeout(60)       # belt and braces for anything using the socket layer


_armed_for = RUN_BUDGET_SECONDS


def _watchdog(_sig, _frm):
    # Report the budget actually armed, not the default — a diagnostic line that lies about its
    # own numbers is worse than no line.
    log(f"ABORTED: run exceeded {_armed_for}s and was killed so the next one can start")
    os._exit(1)


def arm_watchdog(seconds: int = RUN_BUDGET_SECONDS) -> None:
    global _armed_for
    _armed_for = seconds
    try:
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(seconds)
    except (AttributeError, ValueError):
        pass                       # not the main thread, or a platform without SIGALRM


# ----------------------------------------------------------------------------
# Auto-reply detection.
#
# A helpdesk autoresponder ("thanks, we got your ticket") is NOT a reply, but it arrives from the
# prospect's own domain and looks exactly like one. Stamping `replied` for it is silent and
# expensive: due_followups() skips any row with `replied` set, so one autoresponder retires a live
# prospect after a single touch and no follow-up can ever be drafted again. Calypsa hit this on
# 2026-09-19.
#
# The asymmetry that shapes the thresholds below: wrongly calling a HUMAN reply automatic kills
# follow-ups AND buries a buying signal, while wrongly calling an autoresponder human only costs
# one unnecessary follow-up. So only high-confidence signals count, and anything ambiguous is
# treated as a real human reply.
AUTO_HEADERS = ("x-autoreply", "x-autorespond", "x-auto-response-suppress", "x-autoreply-domain")
AUTO_PRECEDENCE = ("bulk", "auto_reply", "junk", "list")
AUTO_SUBJECT = ("auto:", "auto-reply", "autoreply", "automatic reply", "out of office",
                "out-of-office", "away from the office", "automated response", "[ticket ")
# Deliberately specific. "thanks" or "received" alone would match real founder replies.
AUTO_BODY = ("we've received your request", "we have received your request",
             "we've received your message", "we have received your message",
             "this is an automated", "this is an automatic", "do not reply to this",
             "please do not reply", "we usually respond within", "we typically respond within",
             "your ticket has been", "a member of our team will",
             "currently out of the office", "i am out of the office", "i'm out of the office")


def headers_of(msg: dict) -> dict:
    """Lower-cased header name -> value. Gmail nests these under payload.headers."""
    out = {}
    for h in ((msg.get("payload") or {}).get("headers") or []):
        name = str(h.get("name") or "").strip().lower()
        if name:
            out[name] = str(h.get("value") or "").strip()
    return out


def auto_reply_reason(msg: dict) -> str:
    """Why this message is an autoresponder, or "" if it reads as a human reply.

    Header evidence is authoritative (RFC 3834 `Auto-Submitted`); subject and body are fallbacks
    for senders that don't set it."""
    h = headers_of(msg)
    submitted = h.get("auto-submitted", "").lower()
    if submitted and submitted != "no":
        return f"Auto-Submitted: {submitted}"
    for name in AUTO_HEADERS:
        if h.get(name):
            return f"{name} header present"
    prec = h.get("precedence", "").lower()
    if prec in AUTO_PRECEDENCE:
        return f"Precedence: {prec}"
    subject = str(msg.get("subject") or "").strip().lower()
    for frag in AUTO_SUBJECT:
        if subject.startswith(frag) or frag in subject:
            return f"subject says {frag!r}"
    body = body_text(msg).lower()
    for frag in AUTO_BODY:
        if frag in body:
            return f"body says {frag!r}"
    return ""


def body_text(msg: dict) -> str:
    """The message body, preferring full text over the preview.

    Quoted history is dropped: our own outbound copy is quoted underneath the reply, and matching
    autoresponder phrases against our own sent words would be nonsense."""
    raw = msg.get("messageText")
    if not raw:
        p = msg.get("preview")
        raw = p.get("body") if isinstance(p, dict) else p
    text = str(raw or "")
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        if stripped.lower().startswith("on ") and stripped.rstrip().endswith("wrote:"):
            break
        lines.append(line)
    return "\n".join(lines)



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


VAULT_GIT = os.path.expanduser("~/.second-brain-vault.git")


def tracker_rows(allow_mirror: bool = False) -> list:
    """The tracker's rows. iCloud evicts vault files to dataless placeholders and reading one can
    fail (04:20 on 2026-09-24: "Resource deadlock avoided"). For MATCHING, the vault git mirror's
    copy stands in. Never for writing back: a stale copy written over the live file would undo
    whatever changed since the last sync, so stamp_replied reads the real file only."""
    try:
        with open(TRACKER, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        if not allow_mirror:
            raise
        import io
        import subprocess
        r = subprocess.run(["git", "--git-dir", VAULT_GIT, "show", "HEAD:Money/prospect-tracker.csv"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            raise
        log("tracker unreadable in iCloud (probably evicted); matching against the vault git mirror")
        return list(csv.DictReader(io.StringIO(r.stdout)))


# ---------------------------------------------------------------------------
# Replies from an address the tracker doesn't know.
#
# Matching by sender domain misses a real reply whenever the person answering isn't at the address
# we wrote to: a press@ or info@ desk forwards to an agency or to the founder's own gmail, and they
# answer on our thread from there. The thread is the one thing they can't change, so an unknown
# sender is checked once: if the thread holds a message we SENT, the reply belongs to that
# message's recipient. Bounces thread the same way and are not replies. Senders we know are noise
# (our own domain, Google, Hunter, Stripe) are never checked.
# ---------------------------------------------------------------------------
BOUNCE_LOCALS = ("mailer-daemon", "postmaster")
NEVER_A_PROSPECT = {"splitframestudio.com", "google.com", "hunter.io", "stripe.com"}


def thread_recipient(thread_msgs: list) -> str:
    """The address we wrote to in this thread, or "" if we never sent into it."""
    for m in thread_msgs or []:
        if "SENT" not in (m.get("labelIds") or []):
            continue
        to = str(m.get("to") or headers_of(m).get("to", ""))
        to = to.split(",")[0].split("<")[-1].rstrip(">").strip().lower()
        if "@" in to:
            return to
    return ""


def worth_a_thread_check(addr: str) -> bool:
    local, _, dom = addr.partition("@")
    noise = any(dom == d or dom.endswith("." + d) for d in NEVER_A_PROSPECT)
    return bool(dom) and local not in BOUNCE_LOCALS and not noise


def fetch_inbox(c, ent: str, waits=(5, 15)):
    """The inbox + spam read, retried through a dropped connection. Returns None if it never
    answered. A bare Composio APIConnectionError used to kill the run with a traceback that only
    reached the launchd log: on 2026-09-24 about 1 scan in 5 died that way, and reply_watch.log,
    the log anyone reads, showed nothing, so a failed scan looked like a quiet one."""
    last = None
    for wait in (0,) + tuple(waits):
        if wait:
            time.sleep(wait)
        try:
            # Spam too: a reply Gmail files there is still a reply, and the inbox-only read never saw it.
            return c.tools.execute("GMAIL_FETCH_EMAILS", user_id=ent, dangerously_skip_version_check=True,
                                   arguments={"query": "(in:inbox OR in:spam) newer_than:14d",
                                              "max_results": 50, "include_spam_trash": True})
        except Exception as exc:                                  # noqa: BLE001
            last = exc
    log(f"inbox read FAILED {1 + len(waits)} times ({type(last).__name__}): nothing scanned this "
        "run, the next run tries again")
    return None


def fetch_thread(c, ent: str, thread_id: str) -> list:
    res = c.tools.execute("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", user_id=ent,
                          dangerously_skip_version_check=True, arguments={"thread_id": thread_id})
    return (res.get("data") or {}).get("messages") or []


# Shared mailboxes we must never treat as "this whole domain belongs to one prospect". A brand
# whose contact address is @gmail.com is matched on the exact address instead.
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com",
            "me.com", "live.com", "msn.com", "proton.me", "protonmail.com", "gmx.com"}


def prospect_domains(rows) -> dict:
    """domain -> brand for every tracker row (a reply can come from anyone at the brand).

    Reads `email_generic` as well as `email`. Most of the funnel is front-desk brands that carry
    their address in email_generic and leave `email` empty, and their sending domain is often not
    the `domain` column either (Universal Standard mails from .net, Fly By Jing from isetta.co).
    Keying on the other two columns alone left those brands' replies invisible — the same
    email_generic blind spot already found in the send gate, the tracker stamper and the daily cap.
    Freemail domains are excluded here and handled by prospect_addresses()."""
    out = {}
    for r in rows:
        brand = r.get("brand") or ""
        d = (r.get("domain") or "").strip().lower().removeprefix("www.")
        if d and d not in FREEMAIL:
            out[d] = brand or d
        for col in ("email", "email_generic"):
            e = (r.get(col) or "").strip().lower()
            if "@" in e:
                dom = e.split("@", 1)[1]
                if dom not in FREEMAIL:
                    out[dom] = brand or dom
    return out


def exact_addresses(rows) -> dict:
    """Every tracked address -> brand. Checked before any domain match.

    A domain can belong to more than one prospect. The creator lane writes to talent agencies,
    and Dishsoap and Zerbs are both at evolved.gg. prospect_domains() keeps one brand per domain
    (the last row wins), so a reply from dishsoap@evolved.gg would have stamped ZERBS replied.
    That stops the follow-ups to the one who didn't answer and keeps chasing the one who did.
    Matching the exact address first gives the right creator whenever they reply from the address
    we wrote to, which is nearly always."""
    out = {}
    for r in rows:
        for col in ("email", "email_generic"):
            e = (r.get(col) or "").strip().lower()
            if "@" in e:
                out[e] = r.get("brand") or e
    return out


def domain_brands(rows) -> dict:
    """domain -> every brand on it, for a reply from an address we never wrote to (a manager at
    the agency, a colleague at the brand). When several prospects share the domain, every one of
    them is stamped. Chasing someone who answered costs the relationship; pausing one who didn't
    costs a follow-up."""
    out = {}
    for r in rows:
        brand = r.get("brand") or ""
        doms = {(r.get("domain") or "").strip().lower().removeprefix("www.")}
        for col in ("email", "email_generic"):
            e = (r.get(col) or "").strip().lower()
            if "@" in e:
                doms.add(e.split("@", 1)[1])
        for d in doms - {""} - FREEMAIL:
            if brand and brand not in out.setdefault(d, []):
                out[d].append(brand)
    return out


def prospect_addresses(rows) -> dict:
    """exact address -> brand, for prospects reachable only at a shared mailbox.

    A brand whose contact address is @gmail.com cannot be matched on its domain: gmail.com is in
    OWN, so every such prospect was unreachable by the watcher entirely. Matching the full address
    keeps them visible without opening the door to all of Gmail."""
    out = {}
    for r in rows:
        for col in ("email", "email_generic"):
            e = (r.get(col) or "").strip().lower()
            if "@" in e and e.split("@", 1)[1] in FREEMAIL:
                out[e] = r.get("brand") or e
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
    bak = TRACKER + f".bak-replywatch-{when}"
    if not os.path.exists(bak):        # keep the pristine copy when one reply stamps several rows
        shutil.copy2(TRACKER, bak)
    with open(TRACKER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def nudge(title: str, body: str, priority: str = "high", key: str = "splitframe-reply") -> None:
    """Best-effort phone alert. Every outcome is logged.

    The fallback used to succeed or fail in silence, so "did Alex actually get told a prospect
    replied?" could not be answered from the log — the one question this job exists to answer.
    proactive.send_nudge only works inside the server process (it needs proactive.init to wire up
    intake_mod), so the direct ntfy POST is the normal path here, not an emergency one."""
    sys.path.insert(0, os.path.expanduser("~/second-brain/second-brain-chat"))
    try:
        import proactive  # type: ignore
        reason = proactive.send_nudge(key, title, body, priority=priority,
                                      tags="incoming_envelope", force=True)
        if not reason:
            log(f"  nudge sent via send_nudge ({priority})")
            return
        log(f"  send_nudge refused: {reason}")
    except Exception as exc:
        log(f"  send_nudge unavailable: {exc}")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log("  NUDGE NOT SENT: no NTFY_TOPIC configured")
        return
    import urllib.request
    req = urllib.request.Request(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}", data=body.encode(),
                                 headers={"Title": title[:120], "Priority": priority, "Tags": "incoming_envelope"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log(f"  nudge sent via ntfy ({priority})")
    except Exception as exc:
        log(f"  NUDGE NOT SENT: ntfy failed: {exc}")



def _beat(note: str = "") -> None:
    """Liveness into the shared store so the always-on server can see this Mac job."""
    try:
        sys.path.insert(0, CHAT if "CHAT" in globals() else os.path.expanduser("~/second-brain/second-brain-chat"))
        import intake, monitor
        from supabase import create_client
        if intake.supabase is None:
            intake.supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        monitor.supabase = intake.supabase
        monitor.beat("reply-watch", 7200, note)
    except Exception:
        pass

def _refresh_funnel() -> None:
    """Keep Money/Funnel — <date>.md current: after a reply is stamped, the report already says
    which close, which kind of inbox and which wave it answered. It never takes the scan down: a
    stale report costs nothing, a dead reply watcher costs the reply."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "funnel_report", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "funnel_report.py"))
        fr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fr)
        path = fr.refresh()
        if path:
            log(f"funnel report refreshed: {os.path.basename(path)}")
    except Exception as exc:                          # noqa: BLE001
        log(f"funnel report not refreshed ({type(exc).__name__}: {str(exc)[:80]})")


def main() -> int:
    arm_watchdog()
    from composio import Composio  # type: ignore
    c = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    ent = os.environ.get("STUDIO_GMAIL_ENTITY")
    rows = tracker_rows(allow_mirror=True)
    domains = prospect_domains(rows)
    addresses = prospect_addresses(rows)
    exact = exact_addresses(rows)
    shared = domain_brands(rows)
    st = load_state()
    seen = set(st.get("seen", []))
    checked = set(st.get("checked", []))     # unknown senders whose thread was already looked at
    res = fetch_inbox(c, ent)
    if res is None:
        _beat("inbox read failed")
        return 1
    msgs = (res.get("data") or {}).get("messages") or []
    hits = 0
    autos = 0
    for m in msgs:
        mid = m.get("messageId") or m.get("id")
        sender = str(m.get("sender") or "")
        addr = sender.split("<")[-1].rstrip(">").strip().lower()
        dom = addr.split("@", 1)[1] if "@" in addr else ""
        if not mid or mid in seen or not dom:
            continue
        # Exact address first: a prospect at a freemail mailbox is invisible to the domain map, and
        # a domain can belong to more than one prospect (see exact_addresses).
        brand = exact.get(addr) or addresses.get(addr) or (None if dom in OWN else domains.get(dom))
        via = ""
        if not brand and mid not in checked and m.get("threadId") and worth_a_thread_check(addr):
            checked.add(mid)
            try:
                to = thread_recipient(fetch_thread(c, ent, m["threadId"]))
            except Exception as exc:                         # noqa: BLE001
                checked.discard(mid)                         # look again next run
                log(f"thread check failed for {addr} ({type(exc).__name__}); will retry")
                to = ""
            if to:
                tdom = to.split("@", 1)[1]
                brand = exact.get(to) or addresses.get(to) or (None if tdom in OWN else domains.get(tdom))
                via = f" (on our thread to {to})" if brand else ""
        if not brand:
            continue
        if via or addr in exact or addr in addresses:
            brands = [brand]
        else:
            brands = shared.get(dom) or [brand]
        brand = " / ".join(brands)
        subject = str(m.get("subject") or "")[:80]
        preview = body_text(m)
        when = datetime.now().strftime("%Y-%m-%d")
        auto = auto_reply_reason(m)
        if auto:
            # Not a reply. Do NOT stamp `replied` — that would retire a live prospect after one
            # touch. Still worth saying out loud: it proves the address is real and monitored,
            # which is the deliverability signal the lane otherwise has no way to observe.
            log(f"AUTO-REPLY from {brand} <{addr}>{via}: {subject} [{auto}] — follow-ups left open")
            autos += 1
        else:
            log(f"REPLY from {brand} <{addr}>{via}: {subject}")
            for b in brands:
                try:
                    stamp_replied(b, when)
                except OSError as exc:
                    # The nudge below matters more than the stamp: never let an evicted tracker
                    # swallow a reply. The follow-ups for this brand stay armed until it's stamped.
                    log(f"  could not stamp {b} as replied ({type(exc).__name__}): stamp it by hand")
            which = (f"\n(Sent from a domain shared by {brand}; all of them are marked replied so "
                     "nobody gets chased. Un-stamp the ones it isn't.)" if len(brands) > 1 else "")
            nudge(f"{brand} replied", f"{sender}: {subject}\n{preview[:180]}\nReply today. Call card: Money/call-card.md{which}")
            hits += 1
        seen.add(mid)
    st["seen"] = sorted(seen)[-500:]
    st["checked"] = sorted(checked)[-500:]
    st["last_run"] = datetime.now().isoformat()
    save_state(st)
    _beat(f"{len(msgs)} scanned")
    if not hits:
        tail = f", {autos} auto-reply(s) ignored" if autos else ""
        log(f"no prospect replies ({len(msgs)} inbox + spam messages scanned{tail})")
    _refresh_funnel()
    try:
        signal.alarm(0)
    except (AttributeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
