"""
screen_bridge.py — lets the ALWAYS-ON SERVER drive screen control on Alex's Mac.

Why this exists: screen_control.py only works where a real screen is, so it's
Mac-only. But Alex wants one CLARVIS, not two (his call, 2026-07-28, after being
walked through the tradeoff). This bridges the gap without exposing his laptop.

Transport: Supabase, which both nodes already talk to. The Mac POLLS OUT for work
— it never listens on a port, so nothing about his laptop becomes reachable from
the internet, no port-forwarding, no SSH exposure.

    server  --insert screen_command-->  Supabase  <--poll--  Mac relay
    server  <--poll screen_result----   Supabase  --insert-->  Mac relay

SAFETY — the properties that matter are preserved because EXECUTION still happens
inside screen_control.py on the Mac:
  * The physical Escape kill-switch still works; it's local to where the clicking
    happens, and it can't be disabled from the server.
  * start_session() still preflights macOS Accessibility and refuses to arm if
    Escape can't actually be caught.
  * Sessions still self-expire after 5 minutes.
  * type_text() still refuses anything that looks like a credential.
  * The relay only runs while Alex is at the machine (launchd, on login) — if
    the Mac is asleep or the relay is off, commands simply expire unexecuted.

STALENESS is the extra guard this layer adds: a command that has sat in the queue
longer than MAX_COMMAND_AGE is discarded rather than executed. Without it, a
command queued while the Mac was asleep would fire the moment Alex opened his
laptop — clicks landing minutes late, against whatever happens to be on screen.

Accepted risk, stated plainly: the server ingests untrusted content (email, web
pages, scout results). Bridging it to real input means a prompt injection has a
path to Alex's cursor that it did not have before. He accepted this on the
condition that he only uses screen control while sitting at the machine.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

COMMAND_AGENT = "screen_command"
RESULT_AGENT = "screen_result"

# ------------------------------------------------------------
# Command signing. Supabase RLS is currently permissive, so the anon key can
# write to Agent Outputs — which, once a relay executes what it finds there,
# would mean anyone holding that key could move Alex's real mouse. Transport
# access must therefore NOT be sufficient to drive input.
#
# Every command carries an HMAC-SHA256 over its own fields, keyed by a secret
# that lives only in the two nodes' env (never in Supabase). The relay refuses
# anything it can't verify, so writing to the table gets an attacker nothing.
#
# Fails CLOSED and loudly: no secret configured -> the relay executes nothing
# and says why, rather than silently accepting unsigned commands.
# ------------------------------------------------------------
SECRET_ENV = "SCREEN_RELAY_SECRET"


def _secret() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def _canonical(cmd: dict) -> bytes:
    """Stable byte representation of the fields that matter. Excludes 'sig'
    and any mutable bookkeeping (status/claimed_at) so claiming a command
    can't invalidate its own signature."""
    return json.dumps({
        "token": cmd.get("token"),
        "action": cmd.get("action"),
        "payload": cmd.get("payload") or {},
        "sent_at": cmd.get("sent_at"),
    }, sort_keys=True, separators=(",", ":")).encode()


def sign(cmd: dict) -> str:
    return hmac.new(_secret().encode(), _canonical(cmd), hashlib.sha256).hexdigest()


def verify(cmd: dict) -> tuple:
    """(ok, reason). Constant-time compare; never leaks which part mismatched."""
    if not _secret():
        return False, (f"{SECRET_ENV} is not set on this machine — refusing to execute "
                       "anything. Set it in .env (must match the server's value).")
    got = cmd.get("sig")
    if not got:
        return False, "command is unsigned — refusing (someone may be writing to the queue directly)"
    if not hmac.compare_digest(str(got), sign(cmd)):
        return False, "signature mismatch — refusing (forged or tampered command)"
    return True, ""

MAX_COMMAND_AGE = 120.0   # seconds; older commands are discarded, never executed
POLL_INTERVAL = 1.5       # relay poll cadence
DEFAULT_TIMEOUT = 45.0    # how long the server waits for the Mac to answer


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# SERVER SIDE — enqueue a command, wait for the Mac's answer
# ============================================================

def send_command(supabase, action: str, payload: dict = None,
                 timeout: float = DEFAULT_TIMEOUT) -> str:
    """Enqueue one screen action and block until the Mac relay answers (or we
    time out). Returns the relay's result string."""
    if not _secret():
        return (f"Screen control is not configured: {SECRET_ENV} is missing on the server. "
                "Set it (same value as the Mac's .env) — commands are refused without it.")
    token = uuid.uuid4().hex
    cmd = {"token": token, "action": action, "payload": payload or {},
           "sent_at": _now(), "sent_iso": _iso(), "status": "pending"}
    cmd["sig"] = sign(cmd)
    try:
        supabase.table("Agent Outputs").insert(
            {"agent_name": COMMAND_AGENT, "output_text": json.dumps(cmd)}).execute()
    except Exception as e:
        return f"Couldn't reach the screen relay queue: {str(e)[:160]}"

    deadline = _now() + timeout
    while _now() < deadline:
        try:
            rows = (supabase.table("Agent Outputs").select("id,output_text")
                    .eq("agent_name", RESULT_AGENT)
                    .order("id", desc=True).limit(25).execute().data or [])
            for r in rows:
                try:
                    res = json.loads(r["output_text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if res.get("token") == token:
                    return str(res.get("result", ""))
        except Exception:
            pass          # transient read error — keep waiting, don't abort
        time.sleep(1.0)

    return ("No response from the Mac relay (timed out). Screen control needs the "
            "relay running on the Mac and the machine awake — if the laptop is "
            "closed or asleep, nothing was executed.")


# ============================================================
# MAC SIDE — poll for work, execute locally, answer
# ============================================================

def _claim(supabase, row_id: int, cmd: dict) -> bool:
    """Mark a command claimed so a second relay can't double-execute it."""
    cmd["status"] = "claimed"
    cmd["claimed_at"] = _iso()
    try:
        supabase.table("Agent Outputs").update(
            {"output_text": json.dumps(cmd)}).eq("id", row_id).execute()
        return True
    except Exception:
        return False


def _answer(supabase, token: str, result: str):
    try:
        supabase.table("Agent Outputs").insert({
            "agent_name": RESULT_AGENT,
            "output_text": json.dumps({"token": token, "result": result[:8000],
                                        "at": _iso()}),
        }).execute()
    except Exception:
        pass


def _execute(action: str, payload: dict, claude_client=None) -> str:
    """Run one action through the real screen_control module (Mac-only)."""
    import screen_control

    if action == "screenshot":
        # Tool results are plain strings, so a raw image can't be returned.
        # Route through screen_watch's vision instead: capture + describe, so
        # CLARVIS can actually SEE the screen before deciding where to click.
        try:
            import screen_watch
            if claude_client is None:
                return "Screenshot skipped: no vision client available on the relay."
            q = payload.get("question") or (
                "Describe what is on screen: the frontmost app/window, and any "
                "buttons, fields, or links worth acting on. Give approximate "
                "pixel coordinates for anything clickable.")
            return screen_watch.watch_screen(claude_client, question=q)
        except Exception as e:
            return f"Screenshot/vision failed: {str(e)[:200]}"

    tool = {
        "start": "screen_control_start",
        "stop": "screen_control_stop",
        "act": "screen_control_act",
    }.get(action)
    if not tool:
        return f"Unknown relay action: {action}"
    return screen_control.handle_tool_call(tool, payload)


def run_relay(supabase, claude_client=None, log=print):
    """Blocking loop — poll Supabase for screen commands and execute them.
    Runs on the Mac only. Ctrl-C to stop."""
    import screen_control

    log(f"[screen-relay] up — polling every {POLL_INTERVAL}s, "
        f"discarding commands older than {MAX_COMMAND_AGE:.0f}s")
    seen = set()
    while True:
        try:
            rows = (supabase.table("Agent Outputs").select("id,output_text")
                    .eq("agent_name", COMMAND_AGENT)
                    .order("id", desc=True).limit(15).execute().data or [])
            for r in reversed(rows):          # oldest first
                rid = r["id"]
                if rid in seen:
                    continue
                try:
                    cmd = json.loads(r["output_text"])
                except (json.JSONDecodeError, TypeError):
                    seen.add(rid); continue
                if cmd.get("status") != "pending":
                    seen.add(rid); continue

                age = _now() - float(cmd.get("sent_at") or 0)
                token = cmd.get("token", "")
                if age > MAX_COMMAND_AGE:
                    # Never execute a stale click — the screen has moved on.
                    seen.add(rid)
                    cmd["status"] = "expired"
                    try:
                        supabase.table("Agent Outputs").update(
                            {"output_text": json.dumps(cmd)}).eq("id", rid).execute()
                    except Exception:
                        pass
                    log(f"[screen-relay] discarded stale command ({age:.0f}s old): {cmd.get('action')}")
                    _answer(supabase, token,
                            f"Command expired ({age:.0f}s old) and was NOT executed — "
                            "the Mac was asleep or the relay was down.")
                    continue

                # Verify BEFORE claiming or executing: holding the Supabase key
                # must not be enough to drive real input.
                ok, why = verify(cmd)
                if not ok:
                    seen.add(rid)
                    cmd["status"] = "rejected"
                    try:
                        supabase.table("Agent Outputs").update(
                            {"output_text": json.dumps(cmd)}).eq("id", rid).execute()
                    except Exception:
                        pass
                    log(f"[screen-relay] REJECTED {cmd.get('action')}: {why}")
                    _answer(supabase, token, f"Command REJECTED and not executed — {why}")
                    continue

                if not _claim(supabase, rid, cmd):
                    continue
                seen.add(rid)
                action = cmd.get("action", "?")
                log(f"[screen-relay] executing: {action} {str(cmd.get('payload'))[:80]}")
                try:
                    result = _execute(action, cmd.get("payload") or {}, claude_client)
                except Exception as e:
                    result = f"Relay execution failed: {str(e)[:200]}"
                log(f"[screen-relay]   -> {result[:100]}")
                _answer(supabase, token, result)

            if len(seen) > 500:
                seen.clear()
        except Exception as e:
            log(f"[screen-relay] poll error: {str(e)[:160]}")
        time.sleep(POLL_INTERVAL)


# Tool schemas the SERVER registers — same shape/wording as the local ones so
# the model behaves identically whichever node it runs on.
TOOL_SCHEMAS = [
    {"name": "screen_control_start",
     "description": ("Arm literal control of Alex's Mac screen (mouse + keyboard), relayed "
                      "from the server to his Mac. HIGH-RISK — only when Alex explicitly asks "
                      "for something that needs his real desktop rather than a webpage "
                      "(browse_web_sandbox handles anything web-shaped). Requires his Mac awake "
                      "with the relay running. Self-expires in 5 min; Alex kills it instantly "
                      "with Escape. Never type passwords, API keys, or payment details."),
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "One sentence: what you're about to do and why."}
     }, "required": ["reason"]}},
    {"name": "screen_control_screenshot",
     "description": ("LOOK at Alex's screen and get a description of what's on it, including "
                      "approximate coordinates of clickable things. Do this BEFORE clicking "
                      "anything you haven't already located, and again after any action that "
                      "may have changed the screen."),
     "input_schema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "Optional: what specifically to look for."}
     }}},
    {"name": "screen_control_act",
     "description": ("Perform ONE screen action during an armed session: move, click, type, "
                      "key, hotkey, or scroll. Call screen_control_start first, and look at the "
                      "screen before acting blind."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["move", "click", "type", "key", "hotkey", "scroll"]},
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "text": {"type": "string"},
         "key": {"type": "string"},
         "keys": {"type": "array", "items": {"type": "string"}},
         "amount": {"type": "integer"},
     }, "required": ["kind"]}},
    {"name": "screen_control_stop",
     "description": "End the screen-control session immediately. Always call this when done — never leave a session armed.",
     "input_schema": {"type": "object", "properties": {}}},
]


def handle_tool_call(supabase, name: str, tool_input: dict) -> str:
    """Server-side dispatch: turn a tool call into a relayed command."""
    action = {"screen_control_start": "start",
              "screen_control_stop": "stop",
              "screen_control_act": "act",
              "screen_control_screenshot": "screenshot"}.get(name)
    if not action:
        return f"Unknown screen tool: {name}"
    # vision round-trips take longer than a click
    timeout = 90.0 if action == "screenshot" else DEFAULT_TIMEOUT
    return send_command(supabase, action, tool_input or {}, timeout=timeout)
