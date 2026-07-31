# VIBE_CODE_AUDIT.md — CLARVIS vs. the documented failure modes of AI-built apps

Audited 2026-07-31. Method: take the flaw list from published research on vibe-coded
apps, then **test each against this codebase** — greps, real payloads in a real
browser, live probes. Not a vibes-based review.

Headline: **1 real vulnerability found and fixed (SSRF).** Everything else on the
list was either already handled or doesn't apply. That is an unusually good result
against a body of research where ~1 in 3 scanned apps ships a serious hole.

---

## Security flaws (the published checklist)

| Flaw | Verdict | Evidence |
|---|---|---|
| **SSRF** | ⚠️ **FOUND — fixed** | `web_fetch` had no scheme/IP checks and `follow_redirects=True`. An autonomous task acting on untrusted web text could be talked into fetching `169.254.169.254` (Hetzner metadata), `localhost`, or the private network. Now: http(s) only, every resolved address must be public, redirects followed manually so each hop is re-checked (max 5), refusals audited. 14 tests. |
| Hardcoded secrets | ✅ clean | No key patterns in tracked code; **0** matches across all of git history; `.env` gitignored and untracked (already suite-enforced). |
| SQL injection | ✅ clean | Every query parameterized. The one f-string (`job_queue._set`) interpolates only literal column names from three internal call sites — verified, not user-reachable. |
| XSS | ✅ clean | `renderMarkdown` escapes `& < >` **before** any markup, and links are regex-restricted to `http(s)`. Verified by rendering 10 payloads (script/img-onerror/svg-onload/iframe/javascript:/fence-escape/nested) in a real DOM: **all inert, zero alerts**. |
| Broken access control | ✅ clean | Global `before_request` gate — deny-by-default, per-endpoint exemptions are explicit (`login`, `static`, `api_version`). Not per-route opt-in, which is where these bugs usually live. |
| IDOR | ➖ N/A | Single-user system; no per-user object ownership to confuse. |
| Hallucinated / slopsquatted deps | ✅ clean | All 16 requirements resolve to real packages (`gunicorn` is real, just server-only). |
| Broken auth / brute force | ✅ handled | `login_limiter`: 5 wrong/IP → 15-min lock, 20 global → all locked, checked **before** compare. |
| CORS misconfiguration | ✅ N/A | No CORS headers set at all — nothing cross-origin is exposed. |
| Cookie flags | ✅ handled | HttpOnly + SameSite=Lax always, Secure env-driven (on for HTTPS server). |
| Debug mode in prod | ✅ clean | Server runs gunicorn; `debug=True` exists only on the local dev path. |
| Prompt injection | ✅ designed-for | Every untrusted surface (web, mail, iMessage, repos) wrapped in explicit `[UNTRUSTED]` markers; the boundary is suite-tested (`suite_injection`). The SSRF fix closes the one place injection could reach the *network*. |
| Unsafe deserialization | ✅ clean | No `pickle.loads`, no `yaml.load`, no `eval`/`exec` on external data. |
| Arbitrary shell | ✅ gated | The one `shell=True` sits **behind the dashboard approval gate** — it cannot run un-approved (structural, suite-tested). |
| Path traversal | ✅ handled | File tools resolve `realpath` and enforce a home-directory boundary; traversal rejection is suite-tested. |

## Quality flaws (the other half of the research)

| Flaw | Verdict |
|---|---|
| **No tests** — the #1 marker | ✅ **435 checks**, run before every commit. The opposite of the failure mode. |
| Silent error swallowing | ✅ **This was the project's actual disease** — found and cured this week (scout dead 8 days). Now: truncation raises, heartbeats alert, failures audited. |
| "Works but nobody knows why" | ✅ Code is comment-dense with *rationale* (why, not what); handoffs + BUILD_LOG record decisions. |
| Giant files | ⚠️ **`app.py` is ~5,600 lines** — the one genuine structural debt. Not urgent, but every tool makes the eventual split harder. Plan: extract the tool registry during the post-audit prune. |
| Dead code | ⚠️ 93 tools, 12 used on the Mac — see `TOOL_AUDIT.md`. Blocked on cross-node data (mirror shipped 07-30). |
| Unpinned deps | ⚠️ **Only 4 of 16 pinned.** A silent upstream breaking change can't be told from our own bug. Low urgency (single-user, quick rollback) but worth a `pip freeze` pass. |
| Duplicated logic | ⚠️ Two `_extract_json` copies (expansion + money) — deliberate, and a suite keeps them honest. |
| Glue code / accidental architecture | ✅ Explicit "sacred" patterns (modular tool shape, HUD_STYLE.md, hard gates) that are enforced by tests rather than convention. |

---

## What to do next

1. **Nothing urgent.** The SSRF fix is committed locally (deploy freeze holds).
2. `pip freeze` the deps into pins — small, mechanical, low risk.
3. Split `app.py` when the tool prune happens; the two are the same job.
