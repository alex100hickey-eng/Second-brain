# Training app (Weekly Schedule PWA)

Alex's training app — the weekly 30-minute-block schedule grid plus workout
cards, daily routines, everyday warmup, and workout library. **The live copy is
the Netlify deploy at https://luminous-madeleine-bf89fa.netlify.app/** — this
folder is a recovered backup (saved 2026-08-16 from the live site, "App version
5 — sync fixed") so the app can be redeployed if Netlify ever loses it. It was
built in an earlier session and previously existed nowhere in git.

Single self-contained HTML file, no build step. All data lives in
localStorage; the built-in Sync feature mirrors everything to any URL speaking
Firebase's REST shape (`PUT`/`GET <base>/trainingDashboard.json`) — which is
now the CLARVIS server's `/training-sync/<token>` endpoint (see
`second-brain-chat/training_sync.py`). Ask CLARVIS in chat for the sync URL
(`get_training_sync_url`).

If you edit this file, redeploy to Netlify AND keep this copy in sync.
