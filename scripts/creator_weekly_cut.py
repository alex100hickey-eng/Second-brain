#!/usr/bin/env python3
"""creator_weekly_cut.py — the weekly deliverable for a signed clip retainer, as one command per streamer.

    python3 scripts/creator_weekly_cut.py twitch:anthonyz            # or kick:konvy
    python3 scripts/creator_weekly_cut.py twitch:anthonyz --out <folder> --n 3 --max-seconds 45 --dry-run

What the offer promises: three vertical captioned clips a week from that week's streams, in a shared folder,
one line each saying where it came from. The moments are the week's top viewer clips (Twitch: public GraphQL
`clips(period: LAST_WEEK, sort: VIEWS_DESC)`; Kick: `/api/v2/channels/<slug>/clips?sort=view&time=week`),
de-duplicated so two clips of the same moment do not both ship. Each is cut by creator_sample_cut.py
(9:16, captions burned per whisper cue, SRT kept beside the file) and listed in `index.md`.
Output defaults to iCloud Drive/ClipBot/creator-deliveries/<login>/<ISO week>/ — never under ClipBot/ready,
which the clipping bot posts from. Read-only against Twitch/Kick; nothing is sent anywhere.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CUT = os.path.join(HERE, "creator_sample_cut.py")
ICLOUD = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
TWITCH_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def parse_target(s: str) -> tuple:
    """'twitch:login' / 'kick:slug' -> (platform, name). Twitch when no prefix."""
    if ":" in s:
        p, n = s.split(":", 1)
        p = p.strip().lower()
        if p not in ("twitch", "kick"):
            raise ValueError(f"unknown platform {p!r}")
        return p, n.strip().lower()
    return "twitch", s.strip().lower()


def week_folder(day: dt.date | None = None) -> str:
    d = day or dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def clip_url(platform: str, name: str, ident: str) -> str:
    return f"https://clips.twitch.tv/{ident}" if platform == "twitch" else f"https://kick.com/{name}/clips/{ident}"


def fetch_twitch_clips(login: str, first: int = 20) -> list:
    q = (f'query {{ user(login: "{login}") {{ clips(first: {first}, criteria: {{period: LAST_WEEK, sort: VIEWS_DESC}}) '
         f'{{ edges {{ node {{ slug title viewCount createdAt durationSeconds videoOffsetSeconds curator {{ login }} video {{ id }} }} }} }} }} }}')
    req = urllib.request.Request("https://gql.twitch.tv/gql", data=json.dumps({"query": q}).encode(),
                                 headers={"Client-Id": TWITCH_CLIENT, "Content-Type": "application/json", "User-Agent": UA})
    d = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
    out = []
    for e in (((d.get("data") or {}).get("user") or {}).get("clips") or {}).get("edges") or []:
        n = e["node"]
        out.append({"id": n["slug"], "title": n.get("title") or "", "views": n.get("viewCount") or 0,
                    "duration": n.get("durationSeconds") or 0, "created": n.get("createdAt") or "",
                    "curator": (n.get("curator") or {}).get("login") or "", "stream": (n.get("video") or {}).get("id") or "",
                    "offset": n.get("videoOffsetSeconds")})
    return out


def fetch_kick_clips(slug: str) -> list:
    req = urllib.request.Request(f"https://kick.com/api/v2/channels/{slug}/clips?cursor=0&sort=view&time=week",
                                 headers={"User-Agent": UA})
    d = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
    out = []
    for c in d.get("clips") or []:
        out.append({"id": c["id"], "title": c.get("title") or "", "views": c.get("view_count") or 0,
                    "duration": c.get("duration") or 0, "created": c.get("created_at") or "",
                    "curator": (c.get("creator") or {}).get("username") or "", "stream": c.get("livestream_id") or "",
                    "offset": None})
    return out


def _created_ts(c: dict) -> float:
    try:
        return dt.datetime.fromisoformat(str(c.get("created")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def pick_moments(clips: list, n: int = 3, min_seconds: int = 8, max_seconds: int = 60) -> list:
    """The top-viewed clips that are distinct moments. Pure.

    Two viewers often clip the same ten seconds (Sequisha's "open the doors" had two cuts at 5k and 2k). A
    delivery with the same moment twice is a wasted third. Two clips are the same moment when they come from
    the same stream and their VOD offsets (Twitch) or clip times (Kick, no offset) sit within 90 s.
    Clips under min_seconds have no room for a hook; clips over max_seconds are trimmed by the caller."""
    picked = []
    for c in sorted(clips, key=lambda c: -(c.get("views") or 0)):
        if (c.get("duration") or 0) < min_seconds:
            continue
        dup = False
        for p in picked:
            if c.get("stream") and c.get("stream") == p.get("stream"):
                if c.get("offset") is not None and p.get("offset") is not None:
                    dup = abs(c["offset"] - p["offset"]) <= 90
                else:
                    dup = abs(_created_ts(c) - _created_ts(p)) <= 90
            if dup:
                break
        if not dup:
            picked.append(c)
        if len(picked) >= n:
            break
    return picked


def index_lines(platform: str, name: str, week: str, items: list) -> str:
    """The one-line-per-clip index the offer promises. Pure."""
    lines = [f"# {name} — {week} ({platform})", "",
             "Three vertical clips from this week's streams. Captions are machine-generated and checked; "
             "post them or don't, every file is yours.", ""]
    for i, (c, fname) in enumerate(items, 1):
        lines.append(f"{i}. `{fname}` — \"{c.get('title') or 'untitled'}\" ({c.get('views', 0):,} views on the original "
                     f"clip, {int(c.get('duration') or 0)} s, clipped {str(c.get('created'))[:10]}"
                     + (f" by {c['curator']}" if c.get("curator") else "") + f") — source: {clip_url(platform, name, c['id'])}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="twitch:<login> or kick:<slug>")
    ap.add_argument("--out", default="")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--max-seconds", type=int, default=45)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    platform, name = parse_target(a.target)
    week = week_folder()
    out = a.out or os.path.join(ICLOUD, "ClipBot", "creator-deliveries", name, week)
    if "/ready/" in os.path.abspath(out).replace("\\", "/") + "/":
        print("refusing to write under a ClipBot ready folder")
        return 2
    clips = fetch_twitch_clips(name) if platform == "twitch" else fetch_kick_clips(name)
    moments = pick_moments(clips, n=a.n, max_seconds=a.max_seconds)
    if not moments:
        print(f"{name}: no public viewer clips in the last 7 days — nothing to cut; say so to the creator, do not invent a moment")
        return 1
    print(f"{name}: {len(clips)} clips this week, {len(moments)} distinct moments picked")
    items = []
    for i, c in enumerate(moments, 1):
        fname = f"{name}_{week}_{i}_{str(c['id'])[:12]}_1080x1920.mp4"
        print(f"  {i}. {c['views']:>6} views {int(c['duration']):>3}s  {c['title'][:50]!r}  {clip_url(platform, name, c['id'])}")
        items.append((c, fname))
        if a.dry_run:
            continue
        os.makedirs(out, exist_ok=True)
        end = min(int(c["duration"] or a.max_seconds), a.max_seconds)
        r = subprocess.run([sys.executable, CUT, clip_url(platform, name, c["id"]), os.path.join(out, fname), "--end", str(end)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
        print("     " + ((r.stdout or "").strip().splitlines() or ["(no output)"])[-1] if r.returncode == 0 else "     CUT FAILED: " + (r.stderr or r.stdout)[-200:])
    if not a.dry_run:
        with open(os.path.join(out, "index.md"), "w", encoding="utf-8") as f:
            f.write(index_lines(platform, name, week, items))
        print(f"index -> {os.path.join(out, 'index.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
