# D1 tracker — pick up here (last touched 2026-08-29)

All committed and pushed to `main`; `3ae2498` is live on the server.

## Done since the last handoff

- UI now shows both fields that were API-only: the **"vs you"** panel per school
  and an **end-of-bench chip** on walk-on roster rows.
- `roster_tier`, `save_me`/`load_me`, `compare_me` are tested. d1 suite 78 → 102.
- Full suite green at 1041.
- **Scoring fix:** crowding counted walk-ons as blockers. Yale returns 6 guards,
  3 of them end-of-bench — scored as a 6-deep room (32.0) when it's really 3
  deep (18.5). Colgate 24.7 → 15.7, Syracuse 39.3 → 30.3. Only SYR/LEM swapped
  rank but every score moved toward accurate.
- The stuck `eac52da` deploy landed; the server runs its own hourly refresh now.

## Open

1. **He still needs to supply his stat line.** `d1_me.json` does not exist —
   `compare_me` returns `has_line: false` and the page shows a prompt instead of
   the panel. He can say a number in chat ("shooting 41 from three") and
   `d1_set_my_line` catches it. Fields: tp_pct, fg_pct, ft_pct, ppg, apg, rpg,
   spg, topg, ast_to, mpg, note. Percentages 0-100. Do NOT seed placeholders —
   a fake line silently produces fake rankings.

2. **Staff / portal research** — workflow `wu5il5brf` was running at handoff:
   one agent per school for coaching staff, who a transfer actually contacts,
   and portal intake history by level. If it didn't finish, re-run from
   `.claude/projects/.../workflows/scripts/d1-staff-and-portal-wf_22dc6dec-6a9.js`.
   Results should be written into a committed `d1_staff.json` and surfaced in
   the tab — "who to talk to" was part of the original ask and the tab currently
   only links to generic staff-directory pages.

3. **Roster staleness resolves itself by November.** 7 of 13 still serve 2025-26
   rosters, which inflates how open they look. The caveat banner is computed, so
   it disappears on its own — nothing to do, just don't be surprised when the
   ranking shifts as classes get posted.

## Context worth not relearning

- **He asked for a tracker, not a feasibility opinion.** He corrected me twice
  for editorializing about whether D3 → high-major is realistic. He is at the
  top of D3, plays a D1 team this coming season, and considers a practice-player
  spot a real path. Track the programs; skip the verdict.
- Freshman guard (PG/SG) on the **Case Western varsity roster (D3)**,
  transferring after 2026-27, arriving **2027-28**.
- The end-of-bench tier exists because he raised the practice-player path — it's
  a different door than a scholarship spot, and the two look identical on a
  roster page.
