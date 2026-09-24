#!/usr/bin/env python3
"""Deliver the statics the offer arm promised, on the follow-up, once Alex approves each one.

Every offer-arm first touch (2026-09-19..21) said "I'll build that static ... free, yours either
way". Nothing in the system built them. The statics now exist
(Money/Clients/spec-ads/qa-*/, rendered by spec_ad.py from each brand's own photo), and each
brand has a follow-up written to carry one (FOLLOWUPS.md in the same folder). Alex marks
`approve` next to a brand in INDEX.md, and only then does its next follow-up become that email
with the PNG attached. Every other brand keeps its normal wording.

Two doors, one set of rules:
  swap        (Mac, run by hand or by the money session) replaces an already-drafted pending
              follow-up with the static version.
  the drafter (splitframe_daily.main, on whichever node) writes the static version straight away
              when a follow-up comes due for an approved brand. It tells the model when a static
              has already gone, so the next touch doesn't offer it again.

    python3 scripts/offer_statics.py status          # verdicts, variants, what would happen
    python3 scripts/offer_statics.py swap            # dry run
    python3 scripts/offer_statics.py swap --apply

The draft is the thing that sends, so how it's built matters. Two traps are designed out:
  - GMAIL_UPDATE_DRAFT drops In-Reply-To/References. An edited follow-up arrives OUTSIDE the
    thread on the recipient's side. So a new draft is always CREATED on the thread, and the
    outbox row is repointed to it.
  - A local path is not an attachment. The PNG is uploaded first (FileUploadable.from_path) and
    the draft is read back: thread, both reply headers and the attachment must all be present,
    or the row is left alone.
Nothing here sends, and nothing deletes a draft: a replaced draft stays in Drafts, referenced by
nothing.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHAT = os.path.join(ROOT, "second-brain-chat")

_SPEC = importlib.util.spec_from_file_location("splitframe_daily", os.path.join(HERE, "splitframe_daily.py"))
_sfd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_sfd)

SPEC_DIR = os.path.join(_sfd.VAULT, "Money", "Clients", "spec-ads")
STATE_KEY = "splitframe:statics"       # address -> {brand, file, touch/row, draft, at, via}
APPROVE = ("approve", "approved", "yes", "ok", "send", "go", "✅")
MIN_WORDS, MAX_WORDS = 30, 160
FIRST_TOUCH_MAX_WORDS = 190     # a first touch carries the observation too; the queue allows 180

# The static-first arm: a NAMED first touch carries the static in the first email, not just a
# follow-up. Two locks, both required: this switch, and Alex's `approve` in the first-touch QA
# folder's INDEX.md. With either missing, every first touch goes out as plain text, as before.
STATIC_FIRST = False
FIRST_TOUCH_PREFIX = "first-touch-qa-"

# The note the drafter adds once a static has gone. Without it the model reads the first touch,
# sees "I'll build that static", and offers it again after it was already delivered.
DELIVERED_NOTE = ("An earlier email in this thread already delivered the free static ad you "
                  "promised, as an attachment. Do NOT offer to build it again or say it's on the "
                  "way. At most, refer back to it in a few words.")


def _c(v) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def latest_qa_dir(spec_dir: str = None, prefix: str = "qa-") -> str:
    spec_dir = spec_dir or SPEC_DIR
    try:
        dirs = sorted(d for d in os.listdir(spec_dir) if d.startswith(prefix))
    except OSError:
        return ""
    return os.path.join(spec_dir, dirs[-1]) if dirs else ""


# ---------------------------------------------------------------- reading Alex's verdicts

def is_approved(verdict: str) -> bool:
    v = _c(verdict).lower()
    return bool(v) and any(v.startswith(w) for w in APPROVE)


def parse_index(text: str) -> dict:
    """brand (lowercase) -> {brand, file, verdict} from INDEX.md's verdict table."""
    out = {}
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not line.strip().startswith("|"):
            continue
        brand, file_, verdict = cells[0], cells[1].strip("`").strip(), cells[2]
        if not brand or brand.lower() == "brand" or set(brand) <= set("-: "):
            continue
        out[brand.lower()] = {"brand": brand, "file": file_, "verdict": verdict}
    return out


def parse_followups(text: str) -> dict:
    """brand (lowercase) -> {brand, to, body} from FOLLOWUPS.md sections."""
    out, brand, to, body, in_block, lines = {}, "", "", [], False, (text or "").splitlines()
    for line in lines:
        if in_block:
            if line.strip().startswith("```"):
                in_block = False
                if brand:
                    out[brand.lower()] = {"brand": brand, "to": to.lower(), "body": "\n".join(body).strip()}
                continue
            body.append(line)
            continue
        if line.startswith("## "):
            brand, to, body = line[3:].strip(), "", []
        elif line.lower().startswith("to:"):
            to = line.split(":", 1)[1].strip()
        elif line.strip().startswith("```text"):
            in_block, body = True, []
    return out


def body_problems(body: str, max_words: int = MAX_WORDS) -> list:
    """Why a variant must not go out. The same fabrication guard as every other email."""
    problems = []
    words = len((body or "").split())
    if not MIN_WORDS <= words <= max_words:
        problems.append(f"{words} words (this email is {MIN_WORDS}-{max_words})")
    if re.search(r"\bAI\b", body or ""):
        problems.append('says "AI"')
    if re.search(r"[{}<>]|TODO|\.\.\.$", body or ""):
        problems.append("looks unfinished (brackets, TODO or a trailing ...)")
    risky = _sfd.fabrication_risk(body or "")
    if risky:
        problems.append("claims work not done: " + ", ".join(risky))
    return problems


def approved_statics(qa_dir: str = None, variants_file: str = "FOLLOWUPS.md",
                     max_words: int = MAX_WORDS) -> tuple:
    """({address: {brand, file, png, body}}, [skipped reasons]) for brands Alex approved."""
    qa_dir = qa_dir if qa_dir is not None else latest_qa_dir()
    if not qa_dir:
        return {}, []
    try:
        with open(os.path.join(qa_dir, "INDEX.md"), encoding="utf-8") as f:
            index = parse_index(f.read())
        with open(os.path.join(qa_dir, variants_file), encoding="utf-8") as f:
            variants = parse_followups(f.read())
    except OSError as exc:
        return {}, [f"cannot read the QA folder ({exc})"]
    ok, skipped = {}, []
    for key, entry in index.items():
        if not is_approved(entry["verdict"]):
            continue
        v = variants.get(key)
        png = os.path.join(qa_dir, entry["file"])
        if not v or not v["to"] or not v["body"]:
            skipped.append(f"{entry['brand']}: approved, but {variants_file} has no email for it")
        elif not os.path.exists(png):
            skipped.append(f"{entry['brand']}: approved, but {entry['file']} is missing")
        elif body_problems(v["body"], max_words):
            skipped.append(f"{entry['brand']}: " + "; ".join(body_problems(v["body"], max_words)))
        else:
            ok[v["to"]] = {"brand": entry["brand"], "file": entry["file"], "png": png,
                           "body": v["body"]}
    return ok, skipped


def plan_for(address: str, delivered: dict, approved: dict) -> str:
    """What the next follow-up to `address` should be: "attach", "delivered" (normal wording,
    told not to re-offer), or "" (normal wording)."""
    a = _c(address).lower()
    if a in (delivered or {}):
        return "delivered"
    if a in (approved or {}):
        return "attach"
    return ""


def pending_followup(open_items: list, address: str):
    """The not-yet-sent follow-up draft to `address`, if one is waiting in the outbox."""
    a = _c(address).lower()
    for it in open_items or []:
        if it.get("kind") != "email_draft" or it.get("sent_at") or it.get("static_attached"):
            continue
        if not _c(it.get("title")).lower().endswith(a):
            continue
        if _c(it.get("detail")).lower().startswith("subject: re:"):
            return it
    return None


# ---------------------------------------------------------------- the draft itself

def create_attach_draft(composio, entity: str, to: str, subject: str, body: str,
                        thread_id: str, png: str, upload=None) -> tuple:
    """(draft_id, problems). Creates a reply draft ON the thread with the PNG attached and
    reads it back. Any problem means: don't use this draft."""
    if not thread_id:
        return "", ["no thread id: it would arrive as a new email, not a reply"]
    if upload is None:
        from composio.core.models._files import FileUploadable     # type: ignore

        def upload(path):
            up = FileUploadable.from_path(client=composio.client, file=path,
                                          tool="GMAIL_CREATE_EMAIL_DRAFT", toolkit="gmail")
            return up.model_dump() if hasattr(up, "model_dump") else dict(up)
    try:
        attachment = upload(png)
        res = composio.tools.execute(
            "GMAIL_CREATE_EMAIL_DRAFT", user_id=entity, dangerously_skip_version_check=True,
            arguments={"recipient_email": to, "subject": subject, "body": body, "is_html": False,
                       "thread_id": thread_id, "attachment": attachment})
    except Exception as exc:                                   # noqa: BLE001
        return "", [f"create failed: {str(exc)[:160]}"]
    if isinstance(res, dict) and res.get("successful") is False:
        return "", [f"create failed: {str(res.get('error'))[:160]}"]
    d = res.get("data", res) if isinstance(res, dict) else {}
    draft_id = d.get("id") or (d.get("response_data") or {}).get("id") or ""
    if not draft_id:
        return "", ["no draft id came back"]
    try:
        got = composio.tools.execute("GMAIL_GET_DRAFT", user_id=entity,
                                     dangerously_skip_version_check=True,
                                     arguments={"draft_id": draft_id, "format": "full"})
        m = (got.get("data") or {}).get("message") or {}
    except Exception as exc:                                   # noqa: BLE001
        return draft_id, [f"read-back failed: {str(exc)[:160]}"]
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in ((m.get("payload") or {}).get("headers") or [])}
    names = [a.get("filename") for a in (m.get("attachmentList") or [])]
    problems = []
    if m.get("threadId") != thread_id:
        problems.append("the draft is not on the original thread")
    if not headers.get("in-reply-to") or not headers.get("references"):
        problems.append("the reply headers are missing: it would arrive outside the thread")
    if os.path.basename(png) not in names:
        problems.append("the static is not attached")
    return draft_id, problems


# ---------------------------------------------------------------- the first-touch door

def approved_first_touch(qa_dir: str = None) -> tuple:
    """Same as approved_statics, from the first-touch QA folder and its FIRST_TOUCH.md."""
    qa_dir = qa_dir if qa_dir is not None else latest_qa_dir(prefix=FIRST_TOUCH_PREFIX)
    return approved_statics(qa_dir, "FIRST_TOUCH.md", FIRST_TOUCH_MAX_WORDS)


def pending_first_touch(queue: list, brand: str, address: str) -> tuple:
    """(entry, why_not). The brand's not-yet-released first touch, if it's addressed to the
    founder the variant was written for. A draft still pointing at the front desk has to be
    re-addressed first (`splitframe_queue.py revise --new-to`)."""
    b, a = _c(brand).lower(), _c(address).lower()
    for e in queue or []:
        if _c(e.get("brand")).lower() != b or e.get("released") or e.get("static_attached"):
            continue
        if _c(e.get("to")).lower() != a:
            return None, (f"queued to {e.get('to')}, not {a}: re-address it first "
                          "(splitframe_queue.py revise --new-to)")
        return e, ""
    return None, "no unreleased first touch in the queue"


def create_first_touch_draft(composio, entity: str, to: str, subject: str, body: str, png: str,
                             upload=None) -> tuple:
    """(draft_id, problems). A NEW email (no thread) with the PNG attached, read back."""
    if upload is None:
        from composio.core.models._files import FileUploadable     # type: ignore

        def upload(path):
            up = FileUploadable.from_path(client=composio.client, file=path,
                                          tool="GMAIL_CREATE_EMAIL_DRAFT", toolkit="gmail")
            return up.model_dump() if hasattr(up, "model_dump") else dict(up)
    try:
        res = composio.tools.execute(
            "GMAIL_CREATE_EMAIL_DRAFT", user_id=entity, dangerously_skip_version_check=True,
            arguments={"recipient_email": to, "subject": subject, "body": body, "is_html": False,
                       "attachment": upload(png)})
    except Exception as exc:                                   # noqa: BLE001
        return "", [f"create failed: {str(exc)[:160]}"]
    if isinstance(res, dict) and res.get("successful") is False:
        return "", [f"create failed: {str(res.get('error'))[:160]}"]
    d = res.get("data", res) if isinstance(res, dict) else {}
    draft_id = d.get("id") or (d.get("response_data") or {}).get("id") or ""
    if not draft_id:
        return "", ["no draft id came back"]
    try:
        got = composio.tools.execute("GMAIL_GET_DRAFT", user_id=entity,
                                     dangerously_skip_version_check=True,
                                     arguments={"draft_id": draft_id, "format": "full"})
        m = (got.get("data") or {}).get("message") or {}
    except Exception as exc:                                   # noqa: BLE001
        return draft_id, [f"read-back failed: {str(exc)[:160]}"]
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in ((m.get("payload") or {}).get("headers") or [])}
    problems = []
    if to.lower() not in headers.get("to", "").lower():
        problems.append("the draft is not addressed to the founder")
    if os.path.basename(png) not in [a.get("filename") for a in (m.get("attachmentList") or [])]:
        problems.append("the static is not attached")
    return draft_id, problems


def _queue_module():
    spec = importlib.util.spec_from_file_location("splitframe_queue", os.path.join(HERE, "splitframe_queue.py"))
    sq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sq)
    return sq


def cmd_swap_first(args) -> int:
    if not STATIC_FIRST:
        print("STATIC_FIRST is off: every first touch goes out as plain text. Switch it on in "
              "scripts/offer_statics.py once Alex approves a first-touch static.")
        return 0
    qa = args.dir or latest_qa_dir(prefix=FIRST_TOUCH_PREFIX)
    ok, skipped = approved_first_touch(qa)
    for s in skipped:
        print(f"skipped: {s}")
    if not ok:
        print("no first-touch static is approved and rendered yet")
        return 0
    intake, _outbox, composio, entity = _env()
    sq = _queue_module()
    q, queue = sq.load_queue()
    for address, info in ok.items():
        entry, why = pending_first_touch(queue, info["brand"], address)
        if not entry:
            print(f"{info['brand']}: {why}")
            continue
        if not args.apply:
            print(f"would attach: {info['brand']} <{address}> -> {info['file']}")
            continue
        new_id, problems = create_first_touch_draft(composio, entity, address,
                                                    _c(entry.get("subject")), info["body"],
                                                    info["png"])
        if problems:
            print(f"NOT attached: {info['brand']}: {'; '.join(problems)} (stray draft {new_id or 'none'})")
            continue
        entry.update({"replaced_draft": entry.get("draft_id"), "draft_id": new_id,
                      "body": info["body"], "static_attached": info["file"]})
        sq.save_queue(q, queue)
        print(f"ATTACHED: {info['brand']} first touch now carries {info['file']} (draft {new_id}; "
              f"the old draft stays in Drafts, unreferenced)")
    return 0


# ---------------------------------------------------------------- the Mac door: swap

def _env():
    sys.path.insert(0, CHAT)
    try:
        from dotenv import load_dotenv                         # type: ignore
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:                                          # noqa: BLE001
        pass
    from supabase import create_client                         # type: ignore
    from composio import Composio                              # type: ignore
    import intake, outbox                                      # type: ignore
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    intake.supabase = sb
    outbox.init(sb)
    return intake, outbox, Composio(api_key=os.environ["COMPOSIO_API_KEY"]), \
        os.environ.get("STUDIO_GMAIL_ENTITY", "")


def cmd_status(args) -> int:
    qa = args.dir or latest_qa_dir()
    ok, skipped = approved_statics(qa)
    print(f"QA folder: {qa or '(none)'}")
    print(f"approved and ready: {', '.join(v['brand'] for v in ok.values()) or 'none yet'}")
    for s in skipped:
        print(f"  skipped: {s}")
    return 0


def cmd_swap(args) -> int:
    qa = args.dir or latest_qa_dir()
    ok, skipped = approved_statics(qa)
    for s in skipped:
        print(f"skipped: {s}")
    if not ok:
        print("nothing approved yet: every follow-up keeps its current wording")
        return 0
    intake, outbox, composio, entity = _env()
    state = intake._load_state(STATE_KEY) or {}
    delivered = state.get("delivered") or {}
    open_items = outbox.open_items()
    for address, info in ok.items():
        if address in delivered:
            print(f"{info['brand']}: static already on its way ({delivered[address].get('at', '')[:16]})")
            continue
        row = pending_followup(open_items, address)
        if not row:
            print(f"{info['brand']}: no follow-up waiting yet. The drafter will attach it when "
                  "the next one comes due")
            continue
        draft = _c(row.get("ref")).split(":")[-1]
        got = composio.tools.execute("GMAIL_GET_DRAFT", user_id=entity,
                                     dangerously_skip_version_check=True,
                                     arguments={"draft_id": draft, "format": "full"})
        m = (got.get("data") or {}).get("message") or {}
        thread = m.get("threadId") or ""
        subject = _c(row.get("detail")).split("\n", 1)[0].removeprefix("Subject:").strip()
        if not args.apply:
            print(f"would swap: {info['brand']} row {row['id']} (sends {row.get('auto_send_at', '')[:16]}) "
                  f"-> static version with {info['file']}")
            continue
        new_id, problems = create_attach_draft(composio, entity, address, subject, info["body"],
                                               thread, info["png"])
        if problems:
            print(f"NOT swapped: {info['brand']}: {'; '.join(problems)} (row left as it was; "
                  f"stray draft {new_id or 'none'})")
            continue
        outbox._write(row["id"], {"ref": f"gmail:studio:{new_id}",
                                  "detail": f"Subject: {subject}\n\n{info['body']}",
                                  "static_attached": info["file"]})
        delivered[address] = {"brand": info["brand"], "file": info["file"], "row": row["id"],
                              "draft": new_id, "replaced": draft, "via": "swap",
                              "at": datetime.now().isoformat()}
        state.update({"key": STATE_KEY, "delivered": delivered})
        intake._save_state(state)
        print(f"SWAPPED: {info['brand']} row {row['id']} now sends the static version "
              f"(draft {new_id}; the old draft {draft} stays in Drafts, unreferenced)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--dir", default=None)
    st.set_defaults(fn=cmd_status)
    sw = sub.add_parser("swap")
    sw.add_argument("--dir", default=None)
    sw.add_argument("--apply", action="store_true")
    sw.set_defaults(fn=cmd_swap)
    sf = sub.add_parser("swap-first", help="attach approved statics to queued first touches")
    sf.add_argument("--dir", default=None)
    sf.add_argument("--apply", action="store_true")
    sf.set_defaults(fn=cmd_swap_first)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
