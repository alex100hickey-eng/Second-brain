# clipbot

Campaign footage in → OpusClip cuts → ffmpeg adds Alex's voice hook + text hook + a distinct trim
per platform → ready-to-post files in iCloud Drive → one nudge a day → Alex posts from his phone.

## Folders (iCloud Drive → Files app on the phone)
- `ClipBot/inbox/<campaign name>/` — drop campaign source videos here; the loop ingests them.
- `ClipBot/hooks/` — recorded hook lines (Voice Memos → Save to Files). Optional `manifest.json`
  `{"file.m4a": "the line"}`; else the filename is the line.
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
