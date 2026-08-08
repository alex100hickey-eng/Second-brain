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
# The agent loop answers once, after up to MAX_STEPS_CAP see->act rounds. What
# really bounds it is screen_control's 5-minute session expiry, so this only has
# to outlast that plus the final model turn.
AGENT_TIMEOUT = 420.0


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
    poll = 0
    while _now() < deadline:
        try:
            q = (supabase.table("Agent Outputs").select("id,output_text")
                 .eq("agent_name", RESULT_AGENT)
                 .order("id", desc=True))
            # Result rows can carry ~MB base64 screenshots; re-downloading all
            # recent ones every second while waiting burned hundreds of MB per
            # screen session. Ask the server for this command's token instead
            # (_answer writes json.dumps, so the byte sequence is stable) and
            # download the payload once. Every 30th poll scans unfiltered as a
            # safety net against an oddly-encoded result row.
            if poll % 30 == 29:
                q = q.limit(25)
            else:
                q = q.ilike("output_text", f'%"token": "{token}"%').limit(3)
            rows = q.execute().data or []
            for r in rows:
                try:
                    res = json.loads(r["output_text"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if res.get("token") == token:
                    return str(res.get("result", ""))
        except Exception:
            pass          # transient read error — keep waiting, don't abort
        poll += 1
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


# Screenshots travel back as base64 PNG in this field, so the old 8k cap would
# shred them. A logical-resolution PNG is well under a few MB; cap generously.
MAX_RESULT_CHARS = 8_000_000


def _answer(supabase, token: str, result: str):
    try:
        supabase.table("Agent Outputs").insert({
            "agent_name": RESULT_AGENT,
            "output_text": json.dumps({"token": token, "result": result[:MAX_RESULT_CHARS],
                                        "at": _iso()}),
        }).execute()
    except Exception:
        pass


def _execute(action: str, payload: dict, claude_client=None) -> str:
    """Run one action through the real screen_control module (Mac-only) and
    return a STRING for transport. screen_control now returns a structured
    image dict for screenshot/act (so the server-side model can SEE the real
    screen 1:1 with click coordinates); we JSON-encode that here and the
    server side decodes it back into an image tool_result."""
    import screen_control

    if action == "agent":
        # The local see->act loop runs HERE, on the Mac. Every step needs a fresh
        # screenshot and a real click, so relaying step-by-step would put a
        # Supabase round trip between each one; instead only the goal and the
        # final report cross the wire. screen_agent still acts exclusively
        # through screen_control, so the Escape kill-switch, session expiry and
        # credential refusal all apply exactly as they do to a single click.
        import screen_agent
        if claude_client is None:
            return ("Screen agent unavailable: the relay has no CLAUDE_API_KEY, so it "
                    "cannot run the local loop. Add it to .env and restart the relay.")
        screen_agent.init(claude_client)
        goal = (payload or {}).get("goal", "")
        steps = (payload or {}).get("max_steps") or screen_agent.MAX_STEPS_DEFAULT
        return screen_agent.run_screen_task(goal, steps)

    tool = {
        "screenshot": "screen_control_screenshot",
        "start": "screen_control_start",
        "stop": "screen_control_stop",
        "act": "screen_control_act",
    }.get(action)
    if not tool:
        return f"Unknown relay action: {action}"
    result = screen_control.handle_tool_call(tool, payload)
    if isinstance(result, dict):
        return json.dumps(result)
    return result


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
_COORD_RULE = ("Coordinates are in the exact pixel space of the screenshot you were just shown "
               "(its top-left is 0,0). Read the target's position straight off that image — do "
               "not rescale or guess. Every screenshot and every action returns the current "
               "screen image, so always look at the latest one before choosing coordinates.")

TOOL_SCHEMAS = [
    {"name": "screen_control_start",
     "description": ("Arm literal control of Alex's Mac screen (mouse + keyboard), relayed "
                      "from the server to his Mac. HIGH-RISK — only when Alex explicitly asks "
                      "for something that needs his real desktop rather than a webpage "
                      "(browse_web_sandbox handles anything web-shaped). Requires his Mac awake "
                      "with the relay running. Returns a first screenshot so you can see the "
                      "screen. Self-expires in 5 min; Alex kills it instantly with Escape. Never "
                      "type passwords, API keys, or payment details."),
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "One sentence: what you're about to do and why."}
     }, "required": ["reason"]}},
    {"name": "screen_control_screenshot",
     "description": ("SEE Alex's screen — returns the current screen as an image. " + _COORD_RULE),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "screen_control_act",
     "description": ("Perform ONE screen action during an armed session, then get back a fresh "
                      "screenshot showing the result. Actions: move, click, double_click, type, "
                      "key, hotkey, scroll. " + _COORD_RULE + " Use double_click to open "
                      "apps/icons/files; single click for buttons and links. For "
                      "click/double_click/move, pass x and y."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["move", "click", "double_click", "type", "key", "hotkey", "scroll"]},
         "x": {"type": "integer", "description": "Target x in screenshot pixels (for move/click/double_click)."},
         "y": {"type": "integer", "description": "Target y in screenshot pixels (for move/click/double_click)."},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "text": {"type": "string", "description": "Text to type (for kind=type)."},
         "key": {"type": "string", "description": "Single key to press, e.g. 'enter', 'esc' (for kind=key)."},
         "keys": {"type": "array", "items": {"type": "string"}, "description": "Hotkey combo, e.g. ['command','space'] (for kind=hotkey)."},
         "amount": {"type": "integer", "description": "Scroll amount; positive up, negative down (for kind=scroll)."},
     }, "required": ["kind"]}},
    {"name": "screen_control_stop",
     "description": "End the screen-control session immediately. Always call this when done — never leave a session armed.",
     "input_schema": {"type": "object", "properties": {}}},

    # The whole see->act loop, run on the Mac in one shot. Prefer this over
    # driving screen_control_act step by step: each relayed step costs a
    # Supabase round trip, whereas this crosses the wire twice in total.
    {"name": "run_screen_task",
     "description": ("Do a multi-step task on Alex's actual Mac screen — HIGH RISK, and only "
                     "when he's explicitly asked for something the sandboxed browser can't do "
                     "(a native app, not a webpage). The whole see->act loop runs on the Mac: "
                     "it takes its own screenshots, clicks, types and verifies each step, then "
                     "reports back once. Much faster than stepping through screen_control_act "
                     "yourself. Alex can kill it any time by pressing Escape, and it self-expires "
                     "after 5 minutes. Requires the Mac relay to be running."),
     "input_schema": {"type": "object", "properties": {
         "goal": {"type": "string",
                  "description": "What to accomplish, concretely, in one or two sentences."},
         "max_steps": {"type": "integer",
                       "description": "Step budget (default 20, cap 40)."},
     }, "required": ["goal"]}},
]


def handle_tool_call(supabase, name: str, tool_input: dict):
    """Server-side dispatch: turn a tool call into a relayed command. Returns a
    plain string for start/stop/errors, or a structured image dict (with an
    '_image_b64' key) for screenshot/act — the chat loop turns that into an
    image tool_result so the model sees the real screen."""
    action = {"screen_control_start": "start",
              "screen_control_stop": "stop",
              "screen_control_act": "act",
              "screen_control_screenshot": "screenshot",
              "run_screen_task": "agent"}.get(name)
    if not action:
        return f"Unknown screen tool: {name}"
    # start/screenshot/act now round-trip a screenshot from the Mac, which is
    # slower and larger than a bare click acknowledgement. The agent loop runs
    # many steps before answering, and screen_control's own 5-minute session
    # expiry is what actually bounds it — this timeout just has to outlast that.
    if action == "agent":
        timeout = AGENT_TIMEOUT
    else:
        timeout = DEFAULT_TIMEOUT if action == "stop" else 90.0
    result = send_command(supabase, action, tool_input or {}, timeout=timeout)
    # The Mac ships image results as JSON; decode back to a dict for the loop.
    if isinstance(result, str) and result.startswith("{") and "_image_b64" in result:
        try:
            return json.loads(result)
        except (json.JSONDecodeError, ValueError):
            pass
    return result
