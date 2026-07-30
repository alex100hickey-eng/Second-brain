"""
screen_control.py — GATED, LOCAL-ONLY screen/cursor control for CLARVIS.

This is deliberately separate from screen_watch.py (which stays pure
watch-only — see its docstring). This module is the opposite: it moves the
mouse, clicks, types, and presses keys on Alex's real Mac. That is a
categorically different risk than reading Gmail or capturing a screenshot —
anything CLARVIS reads while a control session is active (page text, image
content) could contain instructions, and this module is the one place in the
whole codebase where an instruction like that could turn into a real click on
Alex's real, logged-in machine.

Safety model (all four apply every time, no exceptions):

1. OFF BY DEFAULT, EXPLICIT START. Nothing here runs until
   start_session() is called, and it self-expires after SESSION_MAX_SECONDS
   even if nobody calls stop_session().
2. HARD ESCAPE KILL-SWITCH. The moment start_session() runs, a background
   thread (pynput global listener) watches for the physical Escape key,
   independent of whatever the control loop is doing. Escape sets a
   threading.Event that every action checks BEFORE it executes — a slow or
   hung action does not delay the kill-switch, because the listener itself
   does nothing but flip the flag.
3. LOCAL ONLY. This can only run where a real screen exists — the Mac, not
   the server. Gated on task_manager.RUNTIME != "server" so a server deploy
   can never accidentally load this.
4. NARROW ACTIONS, EVERYTHING LOGGED. No arbitrary shell/AppleScript escape
   hatch — just move / click / type / key / scroll, each one audited via
   monitor.report_event so there's a record of exactly what happened.
   Never type a password, API key, or payment detail through this path —
   if a task needs credentials, stop and tell Alex to enter them himself.
"""

import os
import threading
import base64
import time
from io import BytesIO

import pyautogui
from pynput import keyboard

pyautogui.FAILSAFE = True   # native pyautogui failsafe too: mouse to a screen
                             # corner also aborts, on top of our Escape watchdog
pyautogui.PAUSE = 0.05

# How long to wait after an action before grabbing the "here's the result"
# screenshot, so the UI has settled (menus opened, focus moved, etc.).
POST_ACTION_SETTLE = 0.6

SESSION_MAX_SECONDS = 300   # a session self-expires after 5 minutes regardless

_lock = threading.Lock()
_active = False
_stop_event = threading.Event()
_listener = None
_expire_timer = None
_session_log = []


class ScreenControlStopped(Exception):
    """Raised by any action if Escape was pressed or the session expired."""


def _on_press(key):
    if key == keyboard.Key.esc:
        _stop_event.set()


def _audit(action: str, detail: str = "", ok: bool = True):
    _session_log.append({"action": action, "detail": detail, "ok": ok, "ts": time.time()})
    try:
        import monitor
        monitor.report_event("screen-control", "info" if ok else "warning", action, detail)
    except Exception:
        pass


def is_active() -> bool:
    return _active


def _escape_watchdog_will_actually_work() -> bool:
    """Preflight the ONE thing that makes this safe to use at all: can we
    actually receive the physical Escape key? macOS gates global key
    listening behind Accessibility/Input Monitoring permission — without
    it, pynput's listener silently receives nothing, so a session would
    claim to be killable by Escape while it isn't. Refuse to start rather
    than lie about that."""
    try:
        import Quartz
        return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        # Can't confirm — fail closed rather than assume it's fine.
        return False


def start_session(reason: str = "") -> str:
    """Arm a control session. Returns a status string. No-op (returns the
    existing status) if a session is already active — call stop_session()
    first if you want to reset the clock."""
    global _active, _listener, _expire_timer
    try:
        import task_manager
        if getattr(task_manager, "RUNTIME", "local") == "server":
            return "Screen control is disabled on the server — it only makes sense on the Mac that has the real screen."
    except Exception:
        pass

    if not _escape_watchdog_will_actually_work():
        _audit("session_start", "refused: Accessibility permission not granted", ok=False)
        return ("Refusing to start: the Escape kill-switch can't actually catch key presses yet. "
                "Grant permission first — System Settings > Privacy & Security > Accessibility "
                "(and Input Monitoring) > enable it for Terminal (or whatever runs this app) — "
                "then try again. Not starting without a working kill-switch.")

    with _lock:
        if _active:
            return "Screen control is already active."
        _stop_event.clear()
        _session_log.clear()
        _listener = keyboard.Listener(on_press=_on_press)
        _listener.start()
        _expire_timer = threading.Timer(SESSION_MAX_SECONDS, stop_session, kwargs={"reason": "session timeout"})
        _expire_timer.daemon = True
        _expire_timer.start()
        _active = True
    _audit("session_start", reason[:200])
    return (f"Screen control armed for up to {SESSION_MAX_SECONDS // 60} minutes. "
            "Press Escape any time to stop immediately.")


def stop_session(reason: str = "") -> str:
    global _active, _listener, _expire_timer
    with _lock:
        if not _active:
            return "Screen control was not active."
        _active = False
        _stop_event.set()
        if _listener:
            _listener.stop()
            _listener = None
        if _expire_timer:
            _expire_timer.cancel()
            _expire_timer = None
    _audit("session_stop", reason[:200])
    return "Screen control stopped."


def _guard():
    if not _active:
        raise ScreenControlStopped("No active screen-control session — call start_session() first.")
    if _stop_event.is_set():
        stop_session(reason="escape pressed")
        raise ScreenControlStopped("Stopped: Escape was pressed (or the session expired).")


# ---------------- narrow, audited action set ----------------

def move_to(x: int, y: int, duration: float = 0.2):
    _guard()
    pyautogui.moveTo(x, y, duration=min(duration, 1.0))
    _guard()
    _audit("move", f"({x},{y})")


def click(x: int = None, y: int = None, button: str = "left"):
    _guard()
    if x is not None and y is not None:
        pyautogui.moveTo(x, y, duration=0.15)
        _guard()
    pyautogui.click(button=button)
    _audit("click", f"({x},{y}) {button}")


def double_click(x: int = None, y: int = None):
    _guard()
    if x is not None and y is not None:
        pyautogui.moveTo(x, y, duration=0.15)
        _guard()
    pyautogui.doubleClick()
    _audit("double_click", f"({x},{y})")


def type_text(text: str):
    """Types literal text. Refuses anything that looks like a secret being
    typed through this path — that must go through the user's own hands."""
    _guard()
    lowered = text.lower()
    if any(k in lowered for k in ("password", "passwd", "api key", "secret key", "-----begin")):
        _audit("type_text", "refused: looked like a credential", ok=False)
        raise ScreenControlStopped("Refused: this looks like a credential. Type it yourself.")
    pyautogui.typewrite(text, interval=0.02)
    _audit("type_text", f"{len(text)} chars")


def press_key(key: str):
    _guard()
    pyautogui.press(key)
    _audit("press_key", key)


def hotkey(*keys: str):
    _guard()
    pyautogui.hotkey(*keys)
    _audit("hotkey", "+".join(keys))


def scroll(amount: int):
    _guard()
    pyautogui.scroll(amount)
    _audit("scroll", str(amount))


def _logical_size() -> tuple:
    """The coordinate space pyautogui's mouse actually uses (points, not
    physical pixels). On a Retina Mac this is e.g. 1440x900 even though the
    screen is 2880x1800."""
    size = pyautogui.size()
    return int(size.width), int(size.height)


def capture_screen() -> dict:
    """Grab the screen and return it in the SAME coordinate space the mouse
    uses. This is the fix for the Retina bug: pyautogui.screenshot() returns
    physical pixels (2x on Retina) but pyautogui.click() takes logical points,
    so we downscale the capture to the logical size. The image the model sees
    is therefore 1:1 with click coordinates — a pixel it points at in the
    screenshot is exactly where the cursor will go.

    Returns {"image_b64", "width", "height", "blank"}."""
    logical_w, logical_h = _logical_size()
    img = pyautogui.screenshot()
    # Downscale physical -> logical so screenshot pixels == click coordinates.
    if (img.width, img.height) != (logical_w, logical_h):
        try:
            from PIL import Image
            img = img.resize((logical_w, logical_h), Image.LANCZOS)
        except Exception:
            # Without Pillow we can't rescale; fall back to reporting the real
            # capture size so the caller can still map coordinates itself.
            logical_w, logical_h = img.width, img.height

    # Blank/near-uniform capture almost always means Screen Recording
    # permission is missing — flag it rather than hand back a black image.
    blank = False
    try:
        from PIL import ImageStat
        stat = ImageStat.Stat(img.convert("L"))
        blank = (stat.stddev[0] if stat.stddev else 0.0) < 4.0
    except Exception:
        pass

    buf = BytesIO()
    img.save(buf, format="PNG")
    return {
        "image_b64": base64.b64encode(buf.getvalue()).decode(),
        "width": logical_w,
        "height": logical_h,
        "blank": blank,
    }


def screenshot_region() -> dict:
    """Takes a screenshot for CLARVIS to look at before acting (read step,
    not a control step — but gated the same way, since it only makes sense
    mid-session). Returns the structured capture dict from capture_screen()."""
    _guard()
    shot = capture_screen()
    _audit("screenshot", f"{shot['width']}x{shot['height']}" + (" BLANK" if shot["blank"] else ""))
    return shot


def session_status() -> dict:
    return {
        "active": _active,
        "actions": len(_session_log),
        "recent": _session_log[-10:],
    }


# Shared coordinate contract, injected into every action/screenshot tool
# description so the model always knows the rules. Coordinates are in the
# SAME pixel space as the screenshot it just saw (which we've made 1:1 with
# the mouse), so it should read a target's position straight off the image.
_COORD_RULE = ("Coordinates are in the exact pixel space of the screenshot you were just shown "
               "(its top-left is 0,0). Read the target's position straight off that image — do "
               "not rescale or guess. Every screenshot and every action returns the current "
               "screen image, so always look at the latest one before choosing coordinates.")

TOOL_SCHEMAS = [
    {"name": "screen_control_start",
     "description": ("Arm literal control of Alex's Mac screen: mouse and keyboard. HIGH-RISK — "
                      "only use when Alex has explicitly asked for a task that needs it (sandbox "
                      "browsing can't do it — e.g. an action in a native app, not a webpage). "
                      "Self-expires in 5 minutes; Alex can kill it instantly by pressing Escape. "
                      "After arming, call screen_control_screenshot to SEE the screen before you "
                      "touch anything. Never type passwords, API keys, or payment info through "
                      "this — tell Alex to type those himself."),
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "One sentence: what you're about to do and why."}
     }, "required": ["reason"]}},
    {"name": "screen_control_act",
     "description": ("Perform ONE screen action during an active screen-control session, then get back "
                      "a fresh screenshot showing the result. Actions: move, click, double_click, type, "
                      "key, hotkey, scroll. " + _COORD_RULE + " Use double_click to open apps/icons/files; "
                      "single click for buttons and links. For click/double_click/move, pass x and y."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["move", "click", "double_click", "type", "key", "hotkey", "scroll"]},
         "x": {"type": "integer", "description": "Target x in screenshot pixels (for move/click/double_click)."},
         "y": {"type": "integer", "description": "Target y in screenshot pixels (for move/click/double_click)."},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "text": {"type": "string", "description": "Text to type (for kind=type)."},
         "key": {"type": "string", "description": "Single key to press, e.g. 'enter', 'esc' (for kind=key)."},
         "keys": {"type": "array", "items": {"type": "string"}, "description": "Hotkey combo, e.g. ['command','space'] (for kind=hotkey)."},
         "amount": {"type": "integer", "description": "Scroll amount; positive scrolls up, negative down (for kind=scroll)."},
     }, "required": ["kind"]}},
    {"name": "screen_control_screenshot",
     "description": ("See the current screen during an active screen-control session. Returns the screen "
                      "as an image. " + _COORD_RULE),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "screen_control_stop",
     "description": "End the screen-control session immediately. Always call this when the task is done — don't leave a session armed.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _shot_result(note: str) -> dict:
    """Wrap a fresh capture as the structured image result the chat loop turns
    into an image tool_result. On a blank capture, return an error string
    instead so the model gets actionable text, not a black rectangle."""
    shot = capture_screen()
    _audit("screenshot", f"{shot['width']}x{shot['height']}" + (" BLANK" if shot["blank"] else ""))
    if shot["blank"]:
        return ("Screen capture came back blank — this almost always means macOS Screen Recording "
                "permission isn't granted to the app running CLARVIS. Tell Alex to enable it in "
                "System Settings > Privacy & Security > Screen Recording, then restart the app.")
    return {
        "_image_b64": shot["image_b64"],
        "width": shot["width"],
        "height": shot["height"],
        "text": f"{note} Screen is {shot['width']}x{shot['height']} px; coordinates are in this space.",
    }


def handle_tool_call(name: str, tool_input: dict):
    """Returns a plain string for start/stop, or a structured image dict
    (see _shot_result) for screenshot/act so the caller shows the model the
    real screen."""
    try:
        if name == "screen_control_start":
            status = start_session(tool_input.get("reason", ""))
            # If it armed, hand back the first screenshot so the model can see.
            if is_active():
                return _shot_result(status)
            return status
        if name == "screen_control_stop":
            return stop_session(tool_input.get("reason", "task complete"))
        if name == "screen_control_screenshot":
            _guard()
            return _shot_result("Here's the screen.")
        if name == "screen_control_act":
            kind = tool_input.get("kind")
            if kind == "move":
                move_to(tool_input["x"], tool_input["y"])
            elif kind == "click":
                click(tool_input.get("x"), tool_input.get("y"), tool_input.get("button", "left"))
            elif kind == "double_click":
                double_click(tool_input.get("x"), tool_input.get("y"))
            elif kind == "type":
                type_text(tool_input.get("text", ""))
            elif kind == "key":
                press_key(tool_input.get("key", ""))
            elif kind == "hotkey":
                hotkey(*tool_input.get("keys", []))
            elif kind == "scroll":
                scroll(tool_input.get("amount", 0))
            else:
                return f"Unknown action kind: {kind}"
            # Let the UI settle, then show the model the result of its action.
            if not _stop_event.is_set():
                time.sleep(POST_ACTION_SETTLE)
            return _shot_result(f"Did: {kind}.")
    except ScreenControlStopped as e:
        return f"STOPPED — {e}"
    return "Unknown screen-control tool."
