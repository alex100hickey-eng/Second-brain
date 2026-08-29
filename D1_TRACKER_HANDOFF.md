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

2. **Staff / portal research is DONE** — `d1_staff.json` holds 2026-27 staff, the recruiting contact, contact route and portal intake by level for all 13, surfaced in the tab and the chat tool. Re-run the `d1-staff-and-portal` workflow when staffs turn over (every spring). Four schools still have NO identified recruiting lead: Northeastern, UMass, Cornell, Le Moyne. BC publishes no staff emails at all — questionnaire only.

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

## Added 2026-08-29 (late): auto-pull of his own line

`second-brain-chat/cwru_stats.py` parses CWRU's public cumulative stats page and
maps his row onto the tracker's comparison schema. Verified against real 2025-26
data (22 players parse correctly); the live 2026-27 lookup correctly returns
"no stats posted yet" because the season starts in November.

The trap it solves: the two header rows do NOT align one-to-one. The group row
has 16 cells whose colspans sum to 27, while the sub row holds only the 17 cells
belonging to multi-column groups. Columns are therefore resolved by NAME, not
index — the overall and conference tables have different column counts (the
conference one carries an extra AST/G), so a hardcoded index reads turnovers out
of the steals column on one of them.

**Still to wire (module is standalone and functional, just not hooked up):**
1. Call `cwru_stats.sync_me()` from the refresh cycle — `scripts/d1_refresh.py`
   and the server's `_d1_refresh_loop` in app.py. Cheap: one page fetch.
2. Tests. `parse_stats_page`, `_columns` (the colspan case above),
   `clean_name` ("Edwards, Ethan 31 Edwards, Ethan" -> "Edwards, Ethan"),
   `to_line`, and the not-found paths are all untested.
3. A "where I stack up" rollup — across all 13 at once, not per school.

`sync_me` refuses to write a line with no games behind it, so it cannot
overwrite a good line with an empty one once the season starts.

See `SYNERGY_NOTE.md` — Synergy access is pending and he will raise it himself.
