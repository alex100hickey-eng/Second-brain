# NEEDS_ALEX.md — everything blocked on you

Written 2026-07-31 after a long autonomous session. Suite is 435/0; 15 commits
pushed, 1 held locally. Every item below is blocked on something only you can do:
a credential, a physical permission, a judgment call, or your ears.

Ordered so the first item unblocks the value of everything else.

---

## 1. 🔴 THE DEPLOY — your server is 30 commits / 6 days behind

**This is the whole ballgame.** Nothing built since July 25 is live: the screen-control
relay, mail scan worker fix, task-launch fix, 529 API hardening, and all of today's
work (heartbeats, `/api/version`, voice conversation mode, streaming TTS, scout fixes,
phone HUD, SSRF fix).

**Confirmed, not guessed:** the live server's `hud.js` and `hud.css` byte-match commit
`0efd2a7` (2026-07-25). Infra is fine — host pings, port 22 open, Coolify UI answers,
app serves. Deploys just aren't happening.

**Likely origin:** `766819e` (07-25) added `pyautogui`/`pynput` to requirements, which
breaks a headless Linux build. `821d11c` (07-26) fixed it with `sys_platform` markers —
but that fix is itself undeployed. The cure is stuck behind the disease. Three separate
"re-trigger deploy" commits (07-25, 07-28, 07-31) never took, which points at deploys
not *triggering* rather than *failing*.

**Do this (Coolify UI, not SSH):**
1. Coolify → app `second-brain-chat` (uuid `h72tei3gy97z4wlqyqpvuylg`) → **Deployments**
2. Is there *any* attempt since 07-25? If yes → read its logs. If no → it's the trigger.
3. Check **auto-deploy / webhook enabled** on the app. ← my main suspect
4. If the webhook looks dead, reconnect the GitHub source (App "second-brain1")
5. Hit **Redeploy** manually
6. Verify: `curl https://clarvis.178.156.209.40.sslip.io/api/version`
   → expect commit `8854c6c` or later. **Anything else means it didn't take.**

I verified current requirements are server-safe, so the build should succeed once triggered.

---

## 2. 🟡 macOS permissions — screen control literally cannot click

`AXIsProcessTrusted` is `False`. Until this is granted, screen control refuses to start
(by design — it won't pretend the Escape kill-switch works when it can't).

**System Settings → Privacy & Security →**
- **Accessibility** → enable for Terminal (or whatever runs the app)
- **Screen Recording** → same (needed for captures; without it you get black images)

Then restart the app.

---

## 3. 🟡 Talk to voice conversation mode — 2 minutes

Built and unit-tested, but the thresholds are educated guesses until a real voice hits them.
**Tap the mic once** (don't hold) and just talk. Watch for:
- Does it cut you off mid-thought? → `SILENCE_MS` too low
- Does it wait awkwardly after you stop? → too high
- Both knobs are at the top of the VAD block in `templates/index.html`

Also judge **streaming TTS**: replies should start speaking ~4x sooner (measured 530ms vs
2117ms to first audio). Listen for chunk-boundary artifacts.

*Requires #1 first if you're testing on your phone.*

---

## 4. 🟡 Go/no-go: wire in `screen_agent.py`

A finished local computer-use loop — the model sees real screenshots, coordinates map 1:1
to clicks, everything routes through the existing gates (Escape kill-switch, 5-min expiry,
credential refusal). It is **deliberately not wired in**, with a test pinning it that way,
because your rule is that a new capability passes a human once.

Fable 5's recommendation was **go**. It's 3 lines, documented at the bottom of the module.
Say the word and I'll wire it. *(Needs #2 to actually function.)*

---

## 5. 🟢 Quick wins (2 minutes each)

- **`GITHUB_TOKEN`** — currently unset, so the scout gets 10 GitHub searches/min instead of
  5,000/hr. Generate a classic token (public_repo scope is plenty) → add to `.env` and Coolify.
- **SSH key** — run `ssh-copy-id root@178.156.209.40` once from this Mac. Right now there
  are *no* keys here and access is password-only, which is why I couldn't run the restart
  you authorized. With a key, future sessions can do it when you name the command.
- **4 stale expansion findings** sit in the review panel (job scrapers from before the
  scout was re-aimed). Dismiss them or tell me to.

---

## 6. ⏳ Waiting on time, not you

- **Tool prune** — 93 registered, 12 used on the Mac. The cross-node audit mirror shipped
  07-30; give it ~a week of real two-node data, then we prune with evidence. See `TOOL_AUDIT.md`.
- **`app.py` is ~5,600 lines** — real structural debt, not urgent. Best done *with* the prune.
- **Dependency pinning** — only 4 of 16 pinned. I can do this myself; just say when.

---

## What I did NOT need you for

15 commits, 435 passing checks, and one real security hole (SSRF) found and closed —
see `VIBE_CODE_AUDIT.md`, `TOOL_AUDIT.md`, and today's git log.
