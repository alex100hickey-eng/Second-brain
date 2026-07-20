#!/usr/bin/env python3
"""
run_tests.py — the single regression suite for the Second Brain system.

    python run_tests.py                 # offline suite: fast, free, no NEW network calls
    python run_tests.py --live          # ALSO run live tests (real Claude API / web)
    python run_tests.py --only vault,gate,tasks   # run just the named suites

This is the regression bar for every future build. Run the offline suite after any
change; run --live before declaring a milestone done.

WHAT'S COVERED
  vault      — search_notes / read_note / list_recent_notes + read-only guarantee
  gate       — access-code gate (unauth redirect / 401, wrong code, correct code)
  toolkit    — video_toolkit ffmpeg ops (trim, vertical, thumbnail, caption, concat)
  pipeline   — video_processor local stages (probe, frame sampling, transcription)
  synth      — data synthesizer (offline "organize" mode via a fake model client)
  website    — create_website idempotency guard (one request → one build)
  feasibility— feasibility judge output shape (offline) + 3-idea differentiation (--live)
  tasks      — task tracker CRUD + status flow + history (pure local storage)
  security   — no live secrets in code, localhost-only default binding, .env gitignored

OFFLINE DESIGN: anything that would call the Claude API or scrape the web is replaced
with a realistic fake/stub, so the default run is deterministic and costs nothing.
--live exercises the real model/network paths (a small real website build, real video
vision, real synthesis, real feasibility differentiation).

The suite points OBSIDIAN_VAULT_PATH at ./sample_vault BEFORE importing the app, so it
never touches the real Obsidian vault, and it drives the same code paths the chat uses.
"""

import os
import sys
import shutil
import tempfile
import subprocess

# --- make the app + agents importable, and protect the real vault ------------
ROOT = os.path.dirname(os.path.abspath(__file__))
CHAT_DIR = os.path.join(ROOT, "second-brain-chat")
SAMPLE_VAULT = os.path.join(ROOT, "sample_vault")
os.environ.setdefault("OBSIDIAN_VAULT_PATH", SAMPLE_VAULT)
for p in (CHAT_DIR, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# ------------------------------------------------------------------ harness --
_passed = 0
_failed = 0
_failures = []


def section(title):
    print(f"\n\033[1m# {title}\033[0m")


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        _failed += 1
        _failures.append(f"{name}  {detail}")
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def skip(name, why):
    print(f"  \033[33mSKIP\033[0m  {name}  ({why})")


# ------------------------------------------------------------------- fakes ---
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        return _Msg(self._text)

    def stream(self, **kw):  # not used offline, but present for parity
        raise NotImplementedError


class FakeClaude:
    """Stand-in for the Anthropic client for offline tests — returns canned text."""
    def __init__(self, text="# Report\n**Summary** — stub summary.\n\n## Findings\n- point one\n"):
        self.messages = _FakeMessages(text)


# ffmpeg helpers ---------------------------------------------------------------
def _have(binname):
    return shutil.which(binname) is not None


def _make_clip(path, seconds=3, color="red", size="320x240", with_audio=True):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"color=c={color}:s={size}:d={seconds}:r=24"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-pix_fmt", "yuv420p", "-t", str(seconds)]
    if with_audio:
        cmd += ["-shortest"]
    cmd += [path]
    subprocess.run(cmd, check=True, capture_output=True)


# =============================================================================
# SUITES
# =============================================================================
def suite_vault(app, live):
    section("vault tools (search / read / list + read-only guarantee)")
    import hashlib

    def checksum(p):
        h = hashlib.sha256()
        for r, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d != ".obsidian"]
            for fn in sorted(files):
                fp = os.path.join(r, fn)
                h.update(os.path.relpath(fp, p).encode())
                try:
                    with open(fp, "rb") as f:
                        h.update(f.read())
                except OSError:
                    pass
        return h.hexdigest()

    before = checksum(app.OBSIDIAN_VAULT_PATH)

    out = app.handle_tool_call("list_recent_notes", {"n": 3})
    check("list_recent_notes returns 3 notes", out.count("(folder:") == 3, out[:120])

    out = app.handle_tool_call("search_notes", {"query": "clip farming money", "limit": 3})
    check("search_notes ranks the clip-farming note first",
          "clip-farming-strategy.md" in out.split("###")[1], out[:160])
    check("search_notes shows snippets + source", "snippet:" in out and "note:" in out)

    out = app.handle_tool_call("search_notes", {"query": "zzzznomatchzzz", "limit": 3})
    check("search_notes handles no-match gracefully", "No notes matched" in out, out[:120])

    out = app.handle_tool_call("read_note", {"title_or_path": "footbal trainng plan"})
    check("read_note resolves a misspelled title (fuzzy)",
          "football-training-plan.md" in out, out[:120])
    out = app.handle_tool_call("read_note", {"title_or_path": "goals 2026"})
    check("read_note wraps content as data (injection guard)", "not instructions" in out)

    after = checksum(app.OBSIDIAN_VAULT_PATH)
    check("vault byte-for-byte unchanged after all reads", before == after)


def suite_gate(app, live):
    section("access gate (login required, wrong vs right code)")
    if not app.ACCESS_CODE:
        skip("access gate", "ACCESS_CODE not set — gate disabled in this env")
        return
    app.app.config["TESTING"] = True
    c = app.app.test_client()

    r = c.get("/", follow_redirects=False)
    check("unauth GET / redirects to /login", r.status_code == 302 and "/login" in r.headers.get("Location", ""))

    r = c.get("/api/history", follow_redirects=False)
    check("unauth GET /api/* returns 401", r.status_code == 401)

    r = c.post("/login", data={"password": "definitely-wrong-code"}, follow_redirects=False)
    check("wrong code does NOT authenticate", r.status_code == 200)  # re-renders login with error

    r = c.post("/login", data={"password": app.ACCESS_CODE}, follow_redirects=False)
    check("correct code logs in (redirect to /)", r.status_code == 302)
    r = c.get("/api/history", follow_redirects=False)
    check("authed session can reach /api/*", r.status_code == 200)


def suite_toolkit(app, live):
    section("video toolkit (ffmpeg edit ops)")
    if not _have("ffmpeg"):
        skip("video toolkit", "ffmpeg not installed")
        return
    import video_toolkit
    import glob
    # video_toolkit only operates on files INSIDE the project, so the fixtures must
    # live there too. Use a temp dir under media_lib/ and clean up all artifacts after.
    os.makedirs(video_toolkit.OUT_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="sbtest_tk_", dir=video_toolkit.OUT_DIR)
    try:
        a = os.path.join(tmp, "sbtestclipa.mp4")
        b = os.path.join(tmp, "sbtestclipb.mp4")
        _make_clip(a, seconds=4, color="red")
        _make_clip(b, seconds=3, color="blue", with_audio=False)

        out = video_toolkit.run_operation("trim", filename=a, duration=2)
        check("trim produces an output file", "Done: trim" in out and "media_lib/" in out)

        out = video_toolkit.run_operation("vertical", filename=a)
        check("vertical (9:16) produces output", "1080x1920" in out, out[:160])

        out = video_toolkit.run_operation("thumbnail", filename=a)
        check("thumbnail produces output", "Done: thumbnail" in out)

        out = video_toolkit.run_operation("caption", filename=a, text="Test caption")
        check("caption produces output", "Done: caption" in out)

        out = video_toolkit.run_operation("concat", filenames=[a, b])
        check("concat merges mixed clips (audio + no-audio)", "Done: concat" in out, out[:160])

        try:
            video_toolkit.run_operation("caption", filename=a)  # missing text
            check("caption without text raises a clean error", False)
        except video_toolkit.ToolkitError as e:
            check("caption without text raises a clean error", "text" in str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # outputs land in media_lib/ named after the fixture stems — clean them up
        for f in glob.glob(os.path.join(video_toolkit.OUT_DIR, "sbtestclip*")):
            try:
                os.remove(f)
            except OSError:
                pass


def suite_pipeline(app, live):
    section("video pipeline (local stages: probe / frames / transcribe)")
    if not _have("ffmpeg"):
        skip("video pipeline", "ffmpeg not installed")
        return
    import video_processor
    tmp = tempfile.mkdtemp(prefix="sbtest_vp_")
    # analyze_video (the live vision call) only reads files inside inbox/, so stage
    # the fixture there; the local stages take an explicit path and work anywhere.
    os.makedirs(video_processor.INBOX_DIR, exist_ok=True)
    clip = os.path.join(video_processor.INBOX_DIR, "sbtest_sample.mp4")
    try:
        _make_clip(clip, seconds=4, color="green", with_audio=True)

        info = video_processor.probe_video(clip)
        check("probe_video reports duration ~4s", 3.0 <= info["duration"] <= 5.0, str(info))
        check("probe_video detects audio track", info["has_audio"] is True, str(info))

        frames = video_processor.sample_frames(clip, info["duration"], max_frames=4, work_dir=tmp)
        check("sample_frames extracts >=1 frame", len(frames) >= 1 and all(os.path.exists(f) for f in frames))

        # unsupported extension → clean error
        try:
            video_processor.resolve_video_path(os.path.join(tmp, "nope.txt"))
            check("unsupported ext rejected", False)
        except video_processor.VideoError:
            check("unsupported ext rejected with clean error", True)

        if _have("whisper-cli"):
            tr = video_processor.transcribe_audio(clip, info["duration"], work_dir=tmp)
            check("transcribe_audio returns a result dict", isinstance(tr, dict))
        else:
            skip("transcribe_audio", "whisper-cli not installed")

        if live:
            res = video_processor.analyze_video(app.claude, "sbtest_sample.mp4",
                                                "Describe this clip briefly.", max_frames=3)
            check("[live] analyze_video returns non-empty analysis", isinstance(res, str) and len(res) > 20)
        else:
            skip("analyze_video (Claude vision)", "offline — run with --live")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(clip)
        except OSError:
            pass


def suite_synth(app, live):
    section("data synthesizer (offline organize mode)")
    import data_synthesizer_agent as dsa
    tmp = tempfile.mkdtemp(prefix="sbtest_synth_")
    orig_dir = dsa.SYNTH_DIR
    dsa.SYNTH_DIR = tmp
    try:
        fake = FakeClaude("# Standup\n**Summary** — organized notes.\n\n## Themes\n- shipped X\n- blocked on Y\n")
        res = dsa.synthesize(
            "weekly standup notes",
            raw_material="Mon: shipped feature X. Tue: blocked on Y. Wed: fixed Y.",
            mode="text", claude_client=fake, save=True, log=False,
        )
        check("organize mode used no web sources", res["num_sources"] == 0 and res["mode"] == "text")
        check("synthesizer made exactly one model call", fake.messages.calls == 1)
        check("report saved to disk", res["path"] and os.path.exists(res["path"]))
        check("saved report contains the organized content",
              "organized notes" in open(res["path"]).read())

        # web mode with no real fetch: fake client + monkeypatched empty search → graceful
        orig_gather = dsa.gather_web_material
        dsa.gather_web_material = lambda topic, n: []
        try:
            res2 = dsa.synthesize("some obscure topic", mode="web",
                                  claude_client=FakeClaude(), save=False, log=False)
            check("web mode with zero sources still returns a report (no crash)",
                  bool(res2["markdown"]) and res2["num_sources"] == 0)
        finally:
            dsa.gather_web_material = orig_gather

        if live:
            live_res = dsa.synthesize("benefits of a consistent sleep schedule for students",
                                      mode="web", save=False, log=False)
            check("[live] real web synthesis returns a cited report",
                  live_res["num_sources"] >= 1 and "## Sources" in live_res["markdown"])
        else:
            skip("real web synthesis", "offline — run with --live")
    finally:
        dsa.SYNTH_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


def suite_website(app, live):
    section("website agent (idempotency guard: one request → one build)")
    import website_creator_agent as wca

    calls = {"n": 0}

    def fake_build(brief, port=8080, log=True, claude_client=None, supabase_client=None,
                   progress=None, cinematic=False):
        calls["n"] += 1
        d = tempfile.mkdtemp(prefix="sbtest_site_")
        return {
            "slug": "fake-site", "dir": d, "pages": ["index.html", "about.html"],
            "plan": {"name": "Fake Site", "tagline": "a stub", "slug": "fake-site",
                     "design": {"aesthetic": "clean"}},
            "review_notes": "", "port": port,
        }

    orig_build = wca.create_website
    wca.create_website = fake_build
    wca._RECENT_BUILDS.clear()
    try:
        brief = "A one-page site for a campus coffee cart called Bean Loop."
        r1 = wca.create_website_for_chat(brief)
        r2 = wca.create_website_for_chat(brief)  # duplicate call, same request
        check("first build ran", "Built **Fake Site**" in r1)
        check("duplicate identical brief did NOT trigger a second build", calls["n"] == 1, f"builds={calls['n']}")
        check("duplicate call returns the reused-build note", "reused that build" in r2)

        r3 = wca.create_website_for_chat("A totally different site about vintage bikes.")
        check("a different brief DOES build again", calls["n"] == 2, f"builds={calls['n']}")

        check("empty brief is rejected cleanly",
              "need a brief" in wca.create_website_for_chat("   "))
    finally:
        wca.create_website = orig_build
        wca._RECENT_BUILDS.clear()

    if live:
        section("website agent [live] — one small real build")
        r = wca.create_website_for_chat("A single-page site for a student note-taking app called Inkling. Keep it minimal.")
        check("[live] real build reports a saved site", "Saved to" in r and "serve.sh" in r)
    else:
        skip("real website build", "offline — run with --live")


def suite_feasibility(app, live):
    section("feasibility judge (council's third member)")
    # offline: output shape + empty guard, using a canned judge. Also stub the
    # Supabase logger so the offline suite stays side-effect-free (no council rows).
    orig = app.feasibility_judge
    orig_log = app._log_council
    app._log_council = lambda *a, **k: None
    app.feasibility_judge = lambda idea, outcome="", context="": (
        "**Plausibility: 7/10 (possible)** — canned.\n"
        "**Technical feasibility** — fine.\n**Resource realism** — ok.\n"
        "**Causal chain** — a→b; weakest: b.\n**Most likely failure mode** — b fails.\n"
        "**What would raise the rating** — do b first."
    )
    try:
        out = app.assess_feasibility("build a thing", "ship it")
        check("assess_feasibility includes a plausibility rating", "Plausibility:" in out)
        check("assess_feasibility includes weakest-link / failure-mode sections",
              "Causal chain" in out and "failure mode" in out)
        check("empty idea is handled", "Tell me the idea" in app.assess_feasibility(""))
    finally:
        app.feasibility_judge = orig
        app._log_council = orig_log

    if live:
        section("feasibility judge [live] — 3 ideas must differentiate")
        solid = app.feasibility_judge("keep a simple budgeting spreadsheet", "track monthly spend")
        ambitious = app.feasibility_judge("grow a YouTube channel to 10k subs in a year", "10k subs + income")
        impossible = app.feasibility_judge("build a faster-than-light radio in my dorm this semester", "instant interstellar messaging")

        def rating(text):
            import re
            m = re.search(r"Plausibility:\s*(\d+)\s*/\s*10", text)
            return int(m.group(1)) if m else None

        rs, ra, ri = rating(solid), rating(ambitious), rating(impossible)
        check(f"[live] solid idea rates high (got {rs})", rs is not None and rs >= 7)
        check(f"[live] impossible idea rates very low (got {ri})", ri is not None and ri <= 2)
        check(f"[live] ratings are meaningfully ordered (solid {rs} > ambitious {ra} > impossible {ri})",
              None not in (rs, ra, ri) and rs > ra > ri)
        check("[live] impossible idea names the physics/impossibility, not just 'hard'",
              any(w in impossible.lower() for w in ("physic", "relativ", "impossible", "law", "causal")))
    else:
        skip("3-idea differentiation", "offline — run with --live")


def suite_tasks(app, live):
    section("task tracker (CRUD + status flow + history)")
    try:
        import task_tracker
    except ImportError:
        skip("task tracker", "task_tracker module not present yet")
        return
    tmp = tempfile.mkdtemp(prefix="sbtest_tasks_")
    db = os.path.join(tmp, "tasks.db")
    tt = task_tracker.TaskTracker(db)
    try:
        t = tt.create("Ship the dashboard", "Build the home screen")
        check("create returns a task with an id + default status 'idea'",
              t.get("id") and t.get("status") == "idea")

        tt.update_status(t["id"], "evaluating", note="sent to council")
        got = tt.get(t["id"])
        check("update_status changes status", got["status"] == "evaluating")
        check("status change is recorded in history",
              any(h.get("to") == "evaluating" for h in got["history"]))

        bad = tt.update_status(t["id"], "not-a-real-status")
        check("invalid status is rejected", bad is None or bad.get("error"))

        tt.create("Second task", "another")
        allt = tt.list()
        check("list returns all tasks", len(allt) >= 2)
        opent = tt.list(status="evaluating")
        check("list filters by status", all(x["status"] == "evaluating" for x in opent) and len(opent) == 1)

        tt.add_note(t["id"], "a free-form note")
        got = tt.get(t["id"])
        check("add_note appends to history", any(h.get("note") == "a free-form note" for h in got["history"]))

        # persistence across instances
        tt2 = task_tracker.TaskTracker(db)
        check("tasks persist across tracker instances", tt2.get(t["id"])["title"] == "Ship the dashboard")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def suite_security(app, live):
    section("security invariants")
    # 1. no live secret VALUES hardcoded in any project .py file
    secret_vals = [v for v in (
        os.environ.get("CLAUDE_API_KEY"), os.environ.get("SUPABASE_KEY"),
        os.environ.get("COMPOSIO_API_KEY"), os.environ.get("FLASK_SECRET_KEY"),
        os.environ.get("ACCESS_CODE"),
    ) if v and len(v) >= 12]
    leaked = []
    for r, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "_archive", "node_modules", ".venv")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(r, fn)
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for sv in secret_vals:
                if sv in text:
                    leaked.append((os.path.relpath(fp, ROOT), sv[:6] + "…"))
    check("no live secret value appears in any .py file", not leaked, str(leaked))

    # 2. localhost-only default binding + debug off, in the app entrypoint
    app_src = open(os.path.join(CHAT_DIR, "app.py"), encoding="utf-8").read()
    check("default HOST is 127.0.0.1", 'os.environ.get("HOST", "127.0.0.1")' in app_src)
    check("debug defaults OFF", 'os.environ.get("FLASK_DEBUG", "0")' in app_src)

    # 3. .env is gitignored and untracked
    ci = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    check(".env is gitignored", ci.stdout.strip() == ".env")
    ls = subprocess.run(["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True)
    check(".env is NOT tracked by git", ls.stdout.strip() == "")


SUITES = {
    "vault": suite_vault,
    "gate": suite_gate,
    "toolkit": suite_toolkit,
    "pipeline": suite_pipeline,
    "synth": suite_synth,
    "website": suite_website,
    "feasibility": suite_feasibility,
    "tasks": suite_tasks,
    "security": suite_security,
}


def main():
    live = "--live" in sys.argv
    only = None
    for a in sys.argv:
        if a.startswith("--only"):
            val = a.split("=", 1)[1] if "=" in a else (sys.argv[sys.argv.index(a) + 1] if sys.argv.index(a) + 1 < len(sys.argv) else "")
            only = {s.strip() for s in val.split(",") if s.strip()}

    print("Second Brain — regression suite")
    print(f"  mode: {'LIVE (real API/network)' if live else 'offline (fast, no new network calls)'}")
    print(f"  vault under test: {os.environ['OBSIDIAN_VAULT_PATH']}")
    print("  importing app (starts workers; may print startup warnings)…")
    import app  # noqa: E402 — imported after env is set

    for name, fn in SUITES.items():
        if only and name not in only:
            continue
        try:
            fn(app, live)
        except Exception as e:
            import traceback
            check(f"suite '{name}' ran without crashing", False, f"{e}\n{traceback.format_exc()}")

    print(f"\n{'='*60}")
    print(f"  {_passed} passed, {_failed} failed")
    if _failures:
        print("  failures:")
        for f in _failures:
            print(f"    - {f}")
    print(f"{'='*60}")
    # The app import starts background daemon threads; exit explicitly.
    os._exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
