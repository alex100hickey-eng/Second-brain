# clipbot

Campaign footage in → OpusClip cuts → ffmpeg adds Alex's voice hook + text hook + a distinct trim
per platform → ready-to-post files in iCloud Drive → one nudge a day → Alex posts from his phone.

## Folders (iCloud Drive → Files app on the phone)
- `ClipBot/inbox/<campaign name>/` — drop campaign source videos here; the loop ingests them. A
  `urls.txt` in the same folder (`<url> <minutes>` per line) ingests Drive/YouTube links too.
- `ClipBot/hooks/` — recorded hook lines (Voice Memos → Save to Files). Optional `manifest.json`
  `{"file.m4a": "the line"}`; else the filename is the line. `RECORD_THESE.txt` (30 lines) is
  written there until the first recording exists.
- `ClipBot/ready/<tiktok|shorts|reels|facebook>/` — finished clips + a `.txt` caption beside each
  with the post window and the `posted` command. Posted ones move to `ready/_posted/`.

## Run
```bash
cd ~/second-brain/second-brain-chat
python3 -m clipbot.runner status
python3 -m clipbot.runner campaign add --name "Vyro MrBeast" --marketplace vyro --rate 3 --hashtags "#vyro" \
    --prompt "the funniest 20-40 second moments with a clear payoff, no context needed"
python3 -m clipbot.runner ingest --campaign "Vyro MrBeast" --file ~/Downloads/source.mp4
python3 -m clipbot.runner process
python3 -m clipbot.runner posted --variant 12 --url https://www.tiktok.com/@you/video/123
python3 -m clipbot.runner views --variant 12 --views 4200 --qualified 1600 --approved 4.8
python3 -m clipbot.runner report
python3 -m clipbot.runner prune --keep-days 14   # delete HD/variant files whose posts are done
```
Tests: `python3 -m pytest test_clipbot.py -q` (ffmpeg smoke runs if ffmpeg is installed).

## Guardrails
- Credits governor: refuses an ingest past `weekly_credit_budget` (300) or OpusClip's monthly API cap (900).
- Kill switch: `touch clipbot/KILL` idles the loop.
- Nothing is ever posted by this code in `folder` mode. The `opus` driver (OpusClip's scheduler) is
  the only path that posts, and it stays off until switched on in `config.json`.

## Needs from Alex
`OPUSCLIP_API_KEY` in `~/second-brain/.env` (dashboard → API key; Pro/Max). Campaign signups
(Vyro, Whop). The posting accounts. Twenty recorded hook lines to start.

## Per-campaign brief rules (added 2026-09-12)
Every Vyro/Whop brief differs on what you may change. `campaign add` takes the brief's rules and the
pipeline obeys them per campaign (stored as json in `campaigns.rules`, defaults in `config.DEFAULT_RULES`):

| flag | what it does |
|---|---|
| `--no-voice` | no voice-hook audio ("do not change the audio of the clip") |
| `--no-text-hook` | no on-screen hook card (subtitles already burned in / no added text) |
| `--no-extra-tags` | caption carries only the campaign's hashtags, none from OpusClip |
| `--caption "…"` | mandatory caption line, verbatim, on its own paragraph |
| `--tag @account` | account tag placed before the hashtags |
| `--min-seconds N` / `--max-seconds N` | OpusClip is asked for that window; clips outside it are skipped after download |
| `--brand-template ID` | OpusClip brand template for this campaign (captions on/off live in the template) |
| `--direct` | inbox files are pre-cut clips: skipped past OpusClip (0 credits), transformed and staged as-is |

`ingest --file X --direct` does the same for a single file. Vyro TV-network briefs (FX) want 30–120 s,
no audio changes, mandatory tune-in line + tag: that is the `FX Adults S2` / `The Shards E6-7` setup.
