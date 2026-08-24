"""
action_links.py — one-tap, credential-free links from a phone notification to the
exact thing Alex has to do.

The problem this solves: every nudge used to end with "open CLARVIS" and a deep
link to /dashboard. That is a THREE-step ask on a locked phone — unlock, find the
right panel, remember what the notification said — and a three-step ask at 9pm is
an ask that doesn't happen. A notification that carries the action itself ("Review
& send", "Mark done") gets done in the gap between phone-unlock and doom-scroll.

The gate this has to pass: the /do page is opened from a notification, so it CANNOT
require the access code (Alex is not typing a password into a lock-screen tap). It
authenticates instead with an unguessable, SIGNED, EXPIRING token in the URL path —
the same shape already trusted for `training_sync_endpoint` and `api_widget`, with
two additions those don't need:

  * the token is HMAC-signed over its payload, so it grants access to ONE item and
    one set of operations — not to the app. Editing the payload invalidates it.
  * it expires. A notification is a moment; a link that outlives the moment is a
    credential lying around in a phone's notification history forever.

The secret is derived from ACCESS_CODE, which differs per node (see CLAUDE.md) —
that's correct here: the server sends the nudges, so the server signs the links,
and a Mac-signed link deliberately doesn't open a server page.

Payload keys are single letters because the token rides in a URL that also rides in
an HTTP header (ntfy's Actions header) — every byte is one closer to a truncated
button that does nothing.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# Kinds the /do page knows how to render. Anything else is refused at mint time so
# a typo surfaces here rather than as a dead link on his phone at 7am.
KIND_INTAKE = "intake"      # an extracted obligation (ref = intake row id)
KIND_TASK = "task"          # a tracked task (ref = task id)
KIND_OUTBOX = "outbox"      # something CLARVIS prepared, waiting on Alex's hand
KIND_OUTBOX_ALL = "outbox_all"   # the whole waiting pile, one page
KIND_APPROVAL = "approval"  # the consequential-action queue (ref = row id)
KIND_STEP = "step"          # an August/money tracker step (ref = step id)

VALID_KINDS = (KIND_INTAKE, KIND_TASK, KIND_OUTBOX, KIND_OUTBOX_ALL,
               KIND_APPROVAL, KIND_STEP)

# Operations a token may authorise. A token carries only the ops its nudge offered,
# so a "mark done" link can never be replayed into a "drop it".
VALID_OPS = ("done", "snooze", "drop", "approve", "deny", "sent")

DEFAULT_TTL_DAYS = 14

_MISSING_SECRET_WARNED = False


def _secret() -> bytes:
    """Signing key. Explicit env var wins; otherwise derive from the access code so
    links survive restarts without any new configuration. With neither, fall back to
    a per-process random key: links work while the process lives and die with it,
    which is the safe failure (dead link) rather than the unsafe one (forgeable)."""
    global _MISSING_SECRET_WARNED
    explicit = os.environ.get("ACTION_LINK_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    code = os.environ.get("ACCESS_CODE") or os.environ.get("JARVIS_PASSWORD") or ""
    if code:
        return hashlib.sha256(f"clarvis-action-link:{code}".encode()).digest()
    if not _MISSING_SECRET_WARNED:
        print("action_links: no ACCESS_CODE/ACTION_LINK_SECRET — action links are "
              "per-process only and will break on restart.")
        _MISSING_SECRET_WARNED = True
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_base() -> str:
    """Origin the phone will actually reach. Same env var the old deep link used."""
    return (os.environ.get("JARVIS_PUBLIC_URL")
            or "https://clarvis.178.156.209.40.sslip.io").rstrip("/")


def mint(kind: str, ref: str = "", ops=(), ttl_days: float = DEFAULT_TTL_DAYS,
         label: str = "") -> str:
    """Sign a token for one item. Returns "" on bad input — callers treat an empty
    token as "no action link available" and fall back to the plain nudge, because a
    malformed link is worse than none."""
    if kind not in VALID_KINDS:
        return ""
    ops = tuple(o for o in ops if o in VALID_OPS)
    payload = {"k": kind, "r": str(ref), "x": int(time.time() + ttl_days * 86400)}
    if ops:
        payload["o"] = list(ops)
    if label:
        payload["l"] = label[:120]
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
    return f"{body}.{sig}"


def verify(token: str) -> dict | None:
    """Payload for a valid, unexpired token, else None. Never raises — this runs on
    an ungated route, so every malformed input has to land as a plain 404."""
    try:
        body, _, sig = (token or "").partition(".")
        if not body or not sig:
            return None
        expect = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(body))
        if not isinstance(payload, dict) or payload.get("k") not in VALID_KINDS:
            return None
        if int(payload.get("x", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def allows(payload: dict, op: str) -> bool:
    return bool(payload) and op in (payload.get("o") or [])


def url(kind: str, ref: str = "", ops=(), ttl_days: float = DEFAULT_TTL_DAYS,
        label: str = "") -> str:
    """Full https://…/do/<token> URL, or "" when it can't be minted."""
    token = mint(kind, ref, ops=ops, ttl_days=ttl_days, label=label)
    return f"{public_base()}/do/{token}" if token else ""


def act_url(kind: str, ref: str, op: str, ttl_days: float = DEFAULT_TTL_DAYS) -> str:
    """A URL that PERFORMS `op` when POSTed to — what an ntfy `http` action button
    calls. One op per token: the button that marks something done cannot be replayed
    as the button that drops it."""
    token = mint(kind, ref, ops=(op,), ttl_days=ttl_days)
    return f"{public_base()}/do/{token}/act?op={op}" if token else ""
