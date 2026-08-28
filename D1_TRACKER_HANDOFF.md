# D1 tracker — pick up here (last touched 2026-08-28)

Everything below is committed and pushed to `main`. Nothing is half-broken;
the only gap is UI display of two new fields.

## Where it stands

Live and working: `/d1` page, `/api/d1`, `D1 targets` deck tile, `d1_tracker`
chat tool, hourly launchd refresh on the Mac.

Commits: `5e520ea` (tracker) → `eac52da` (server refresh worker) →
`b24f131` (his own line + roster tiers).

## Next steps, in order

1. **Wire the UI to two fields that already exist in the API but aren't shown.**
   `templates/d1.html` needs:
   - a "vs you" panel per school from `school.vs_me` — `{has_line, pool_size,
     metrics: {tp_pct: {label, mine, rank, of, beats[], best_in_room}, ...}}`.
     Renders as "3P%: you 41.0 — 1st of 5, beats Payne, Hand, Toews…"
   - `guard.tier` on each table row (`rotation` / `bench` / `end of bench` /
     `unknown`). The `end of bench` tier is the practice-player door he raised.
   Nothing displays these yet; the data is there and correct.

2. **Add tests for the new functions** — `roster_tier`, `load_me`/`save_me`,
   `compare_me`. `test_d1_tracker.py` is at 78 checks and green, but those
   three are currently untested. Note `compare_me` excludes guards with
   `tp_att < 20` from 3P% comparisons (a 0.0 there means no attempts, not a
   cold shooter) — worth pinning.

3. **Run the full suite.** `python3 run_tests.py` was killed mid-run at the
   stopping point, so only `test_d1_tracker.py` (78/78) is confirmed since
   `b24f131`. Expect green; verify before assuming.

4. **He needs to supply his own stat line.** `d1_me.json` does not exist yet —
   `compare_me` returns `has_line: false` until it does. He can just say a
   number in chat ("shooting 41 from three") and the `d1_set_my_line` tool
   catches it, or write the file directly. Fields: tp_pct, fg_pct, ft_pct,
   ppg, apg, rpg, spg, topg, ast_to, mpg, note. Percentages 0-100.

5. **`eac52da` may still not be deployed.** It adds the server's own hourly
   refresh worker. Check `/api/version` — if it shows `5e520ea` or `b24f131`
   without `eac52da` having landed, Coolify's queue jammed and Alex needs to
   click Redeploy. Not urgent: the server still refreshes lazily on page open.

## Context worth not relearning

- **He asked for a tracker, not a feasibility opinion.** He corrected me twice
  for editorializing about whether D3 → high-major is realistic. He knows. He
  is at the top of D3, plays a D1 team this coming season, and considers a
  practice-player spot a real path. Track the programs; skip the verdict.
- He's a freshman guard (PG/SG) on the **Case Western varsity roster (D3)**,
  transferring after 2026-27, arriving **2027-28**.
- A BC research workflow (staff / portal history / roster news) was launched
  and **stopped before finishing** — nothing was returned or saved. Re-run if
  wanted: script at
  `.claude/projects/.../workflows/scripts/bc-transfer-scout-wf_57e26862-466.js`.
  Drop its `d3reality` topic — that's the editorializing he pushed back on.
  Its `staff` and `portal` topics are the genuinely useful ones ("who to talk
  to" was part of the original ask and is still unanswered).
