"""
test_action_links.py — the signed one-tap links and the /do page's action layer.

What's actually at risk here, and therefore what's tested:
  * a FORGEABLE link would be an ungated write endpoint on a public URL;
  * an EXPIRED link is a credential sitting in a phone's notification history;
  * an op the token never granted must not be performable through it;
  * and the notification's Actions header has to be well-formed, because a
    malformed one renders NO buttons at all — a silent regression that would
    look exactly like "the feature didn't ship".
"""

import os
import sys

os.environ["ACTION_LINK_SECRET"] = "test-secret-for-links"
os.environ.setdefault("JARVIS_PUBLIC_URL", "https://clarvis.example")

import action_links as al           # noqa: E402
import do_actions                   # noqa: E402
import proactive                    # noqa: E402
import outbox                       # noqa: E402

PASS, FAIL = "PASS  ", "FAIL  "
_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"{PASS if cond else FAIL} {label}")


# ------------------------------------------------------------------
# 1. Signing
# ------------------------------------------------------------------
def test_signing():
    print("\n=== 1. token signing ===")
    tok = al.mint(al.KIND_TASK, "42", ops=("done", "snooze"))
    payload = al.verify(tok)
    check("a minted token verifies", payload is not None)
    check("payload carries kind and ref",
          payload["k"] == al.KIND_TASK and payload["r"] == "42")
    check("granted ops are allowed", al.allows(payload, "done"))
    check("ungranted ops are refused", not al.allows(payload, "drop"))

    body, _, sig = tok.partition(".")
    forged = al._b64e(b'{"k":"task","o":["drop"],"r":"42","x":9999999999}') + "." + sig
    check("re-signing a swapped payload with an old signature fails",
          al.verify(forged) is None)
    check("truncated token fails", al.verify(tok[:12]) is None)
    check("empty token fails", al.verify("") is None)
    check("garbage fails", al.verify("not-a-token") is None)

    other = al.mint("not_a_kind", "1")
    check("an unknown kind refuses to mint", other == "")

    expired = al.mint(al.KIND_TASK, "42", ops=("done",), ttl_days=-1)
    check("an expired token is rejected", al.verify(expired) is None)

    os.environ["ACTION_LINK_SECRET"] = "a-different-secret"
    check("a token signed with another key is rejected", al.verify(tok) is None)
    os.environ["ACTION_LINK_SECRET"] = "test-secret-for-links"
    check("restoring the key restores the token", al.verify(tok) is not None)


def test_urls():
    print("\n=== 2. URL shapes ===")
    url = al.url(al.KIND_OUTBOX, "7", ops=("done",))
    check("do URL points at /do/<token>", url.startswith("https://clarvis.example/do/"))
    check("do URL's token verifies", al.verify(url.rsplit("/", 1)[1]) is not None)
    act = al.act_url(al.KIND_OUTBOX, "7", "done")
    check("act URL carries the op", act.endswith("/act?op=done"))
    tok = act.split("/do/", 1)[1].split("/act", 1)[0]
    p = al.verify(tok)
    check("an act token grants ONLY its own op",
          al.allows(p, "done") and not al.allows(p, "drop"))
    # A URL has to survive being a header value AND a query string.
    check("token is URL-safe", all(c.isalnum() or c in "-_." for c in tok))


# ------------------------------------------------------------------
# 3. The ntfy Actions header
# ------------------------------------------------------------------
def test_actions_header():
    print("\n=== 3. ntfy Actions header ===")
    h = proactive._actions_header([
        {"kind": "view", "label": "Review & send", "url": "https://x/y"},
        {"kind": "http", "label": "Sent it", "url": "https://x/act?op=done"},
    ])
    check("two buttons render as two ';'-joined clauses", h.count(";") == 1)
    check("view clause is well-formed", h.startswith("view, Review & send, https://x/y"))
    check("http clause declares its method", "method=POST" in h)
    check("http clause clears the notification", "clear=true" in h)

    dirty = proactive._actions_header(
        [{"kind": "view", "label": "Do it, now; really", "url": "https://x"}])
    check("commas/semicolons are stripped from labels — they are the "
          "header's own separators",
          "," not in dirty.split(", ", 1)[1].split(", https")[0]
          and ";" not in dirty)

    over = proactive._actions_header(
        [{"kind": "view", "label": f"b{i}", "url": "https://x"} for i in range(6)])
    check("no more than three buttons are emitted (ntfy's limit)",
          over.count(";") == proactive.MAX_ACTIONS - 1)

    check("a button with no URL is dropped, not emitted broken",
          proactive._actions_header([{"kind": "view", "label": "x", "url": ""}]) == "")
    check("an unknown button kind is dropped",
          proactive._actions_header(
              [{"kind": "delete", "label": "x", "url": "https://x"}]) == "")
    check("no actions -> empty header (never sent)",
          proactive._actions_header(None) == "" and proactive._actions_header([]) == "")

    # The header must survive urllib's latin-1 encode, like Title/Tags already do.
    emoji = proactive._actions_header(
        [{"kind": "view", "label": "Open 📤", "url": "https://x"}])
    check("emoji labels survive the header-safe round-trip",
          proactive._header_safe(emoji).encode("latin-1").decode("utf-8") == emoji)


# ------------------------------------------------------------------
# 4. do_actions: refusing what the token didn't grant
# ------------------------------------------------------------------
class FakeTracker:
    def __init__(self):
        self.tasks = {5: {"id": 5, "title": "Email Coach Staley", "status": "idea",
                          "due": "", "description": "from intake"}}
        self.calls = []

    def get(self, tid):
        return self.tasks.get(int(tid))

    def update_status(self, tid, status, note=""):
        t = self.tasks.get(int(tid))
        if not t:
            return None
        t["status"] = status
        self.calls.append((tid, status, note))
        return t

    def set_due(self, tid, due):
        self.tasks[int(tid)]["due"] = due
        return self.tasks[int(tid)]


def test_perform_gating():
    print("\n=== 4. ops are gated by the token ===")
    tracker = FakeTracker()
    do_actions.init(tracker=tracker)

    p = al.verify(al.mint(al.KIND_TASK, "5", ops=("done",)))
    res = do_actions.perform(p, "drop")
    check("an op the token never granted is refused", not res["ok"])
    check("the refusal does not mutate the task", tracker.tasks[5]["status"] == "idea")

    res = do_actions.perform(p, "done")
    check("the granted op runs", res["ok"])
    check("the task is actually closed", tracker.tasks[5]["status"] == "done")

    gone = al.verify(al.mint(al.KIND_TASK, "999", ops=("done",)))
    res = do_actions.perform(gone, "done")
    check("a vanished task fails soft, never raises", res["ok"] is False)


def test_resolve_views():
    print("\n=== 5. the page always answers what/why/how ===")
    tracker = FakeTracker()
    do_actions.init(tracker=tracker)
    view = do_actions.resolve(al.verify(
        al.mint(al.KIND_TASK, "5", ops=("done", "snooze", "drop"))))
    check("view names the thing", view["title"] == "Email Coach Staley")
    check("view has steps — the whole point of the page", len(view["steps"]) >= 2)
    check("view carries the ops the token granted",
          set(view["ops"]) == {"done", "snooze", "drop"})

    tracker.tasks[5]["status"] = "done"
    view = do_actions.resolve(al.verify(al.mint(al.KIND_TASK, "5", ops=("done",))))
    check("an already-closed item reads as handled, not as an error",
          view["gone"] and "closed" in view["title"].lower())

    # A kind whose backing module isn't initialised must degrade, not explode.
    do_actions.init()
    view = do_actions.resolve(al.verify(al.mint(al.KIND_OUTBOX, "1", ops=("done",))))
    check("an uninitialised backend still returns a renderable view",
          isinstance(view, dict) and view["title"])


# ------------------------------------------------------------------
# 6. outbox lifecycle
# ------------------------------------------------------------------
class FakeSB:
    """Minimal Supabase stand-in: insert/select/update over one in-memory table."""
    def __init__(self):
        self.rows, self._next = [], 1
        self._q = None

    def table(self, name):
        self._table = name
        return self

    def insert(self, row):
        row = dict(row)
        row["id"] = self._next
        self._next += 1
        self.rows.append(row)
        self._result = [row]
        return self

    def select(self, *a):
        self._result = list(self.rows)
        return self

    def eq(self, col, val):
        self._result = [r for r in self._result if r.get(col) == val]
        return self

    def order(self, col, desc=False):
        self._result = sorted(self._result, key=lambda r: r.get(col, 0), reverse=desc)
        return self

    def limit(self, n):
        self._result = self._result[:n]
        return self

    def update(self, changes):
        for r in self._result:
            r.update(changes)
        return self

    def execute(self):
        return type("R", (), {"data": list(self._result)})()


def test_outbox():
    print("\n=== 6. outbox: prepared work that waits on Alex ===")
    sb = FakeSB()
    outbox.init(sb)
    oid = outbox.add("email_draft", "Send the reply to coach@case.edu",
                     detail="Subject: Re: lift times", link="https://mail.google.com",
                     steps=["Open drafts", "Send"], ref="gmail:personal:abc")
    check("an item is filed", isinstance(oid, int))
    check("it shows as open", len(outbox.open_items()) == 1)
    check("a fresh item does NOT nudge yet — he was just in that conversation",
          outbox.nudgeable() == [])

    dup = outbox.add("email_draft", "Send the reply to coach@case.edu",
                     ref="gmail:personal:abc")
    check("a repeated ref collapses onto the same item, never a second nudge",
          dup == oid and len(outbox.open_items()) == 1)

    # Age it past the quiet period.
    import json
    from datetime import timedelta
    row = sb.rows[0]
    item = json.loads(row["output_text"])
    item["created"] = (outbox._now() - timedelta(hours=4)).isoformat()
    row["output_text"] = json.dumps(item)
    check("an aged item becomes nudgeable", len(outbox.nudgeable()) == 1)
    check("summary says how long it's been waiting",
          "waiting" in outbox.summary_line(outbox.open_items()[0]))

    outbox.snooze(oid, hours=2)
    check("snoozing silences it without closing it",
          outbox.nudgeable() == [] and len(outbox.open_items()) == 1)

    outbox.close(oid, outbox.DONE)
    check("closing removes it from open", outbox.open_items() == [])

    do_actions.init(outbox=outbox)
    view = do_actions.resolve(al.verify(al.mint(al.KIND_OUTBOX, str(oid), ops=("done",))))
    check("a closed item's link reads as already handled", view["gone"])


def test_outbox_failsoft():
    print("\n=== 7. outbox never breaks its caller ===")
    outbox.init(None)
    check("no store -> add returns None, does not raise",
          outbox.add("email_draft", "x") is None)
    check("no store -> reads are empty, not exceptions", outbox.open_items() == [])

    class Broken(FakeSB):
        def execute(self):
            raise RuntimeError("supabase down")

    outbox.init(Broken())
    check("a broken store still fails soft on read", outbox.nudgeable() == [])
    check("a broken store still fails soft on write", outbox.add("x", "y") is None)


for t in (test_signing, test_urls, test_actions_header, test_perform_gating,
          test_resolve_views, test_outbox, test_outbox_failsoft):
    t()

print("\n" + "=" * 48)
print(f"{sum(_results)}/{len(_results)} checks passed")
print("=" * 48)
sys.exit(0 if all(_results) else 1)
