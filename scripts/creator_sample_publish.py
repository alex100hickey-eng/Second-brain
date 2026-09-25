#!/usr/bin/env python3
"""Publish one captioned creator sample as a private, noindex page on splitframestudio.com.

    creator_sample_publish.py <sample.mp4> <login> [--title "..."] [--views N] [--no-push] [--site DIR]

Transcodes the 1080x1920 sample to 720x1280 H.264 (CRF 28, faststart, aims under 6 MB), writes
`samples/<login>-<8hex>/index.html` + `clip.mp4` in the site repo, commits, pushes (unless --no-push)
and prints the URL. The slug is login + sha256("splitframe-sample:"+login)[:8], so the URL is not
guessable and stays stable across re-renders. No index page, no nav link, `noindex,nofollow,noarchive`.

Pushing publishes to the public site: run with --no-push unless the person at the keyboard has said
to publish. Never writes anywhere under ClipBot/ready/.
"""
import argparse, hashlib, html, os, subprocess, sys

BIN = "/opt/homebrew/bin"
SITE = os.path.expanduser("~/second-brain/portfolio-site/dist")
BASE_URL = "https://splitframestudio.com/samples/"
MAX_BYTES = 6 * 1024 * 1024

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive"><title>A clip for {name}</title>
<style>body{{margin:0;background:#0b0b0c;color:#f2f2f2;font:16px/1.5 -apple-system,Helvetica,Arial,sans-serif}}main{{max-width:480px;margin:0 auto;padding:24px 16px 48px}}
video{{width:100%;max-height:82vh;background:#000;border-radius:14px;display:block}}h1{{font-size:18px;font-weight:600;margin:16px 0 4px}}p{{margin:6px 0;color:#c9c9c9}}a{{color:#9ad1ff}}small{{color:#8a8a8a}}</style></head>
<body><main><video controls playsinline preload="metadata" src="clip.mp4"></video>
<h1>A clip for {name}</h1><p>{line}</p><p>Yours to post, no strings. <a href="clip.mp4" download>Download the file</a>.</p>
<p><small>Cut by Alex Hickey · Splitframe Studio · <a href="https://splitframestudio.com">splitframestudio.com</a></small></p></main></body></html>
"""


def slug_for(login: str) -> str:
    login = login.lower()
    return login + "-" + hashlib.sha256(("splitframe-sample:" + login).encode()).hexdigest()[:8]


def page_html(name: str, title: str = "", views: int = 0, when: str = "") -> str:
    bits = []
    if when:
        bits.append(f"From your {html.escape(when)} stream")
    else:
        bits.append("From your stream")
    if title:
        bits.append(f": “{html.escape(title)}”")
    if views:
        bits.append(f", a viewer clip with {views:,} views")
    bits.append(", cut vertical with captions.")
    return PAGE.format(name=html.escape(name), line="".join(bits))


def transcode_cmd(src: str, dst: str, crf: int = 28) -> list:
    return [f"{BIN}/ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-vf", "scale=720:1280", "-r", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst]


def transcode(src: str, dst: str, run=None) -> int:
    run = run or subprocess.run
    """Encode at CRF 28; if the file is still over the cap, step CRF up to 34 before giving up."""
    for crf in (28, 30, 32, 34):
        run(transcode_cmd(src, dst, crf), check=True)
        size = os.path.getsize(dst)
        if size <= MAX_BYTES:
            return size
    return size


def refuse_ready(path: str) -> None:
    if "/ClipBot/ready/" in os.path.abspath(path) + "/":
        raise SystemExit("refusing to touch ClipBot/ready/ (clipbot posts from there)")


def git(site: str, *args, run=None):
    return (run or subprocess.run)(["git", "-C", site, *args], check=True, capture_output=True, text=True)


def publish(sample: str, login: str, *, title="", views=0, when="", site=SITE, push=True,
            run=None) -> str:
    run = run or subprocess.run
    refuse_ready(sample); refuse_ready(site)
    if not sample or not os.path.isfile(sample):
        raise SystemExit(f"sample file not found: {sample!r} (an empty path here usually means a shell loop lost its stdin)")
    if not os.path.isdir(os.path.join(site, ".git")):
        raise SystemExit(f"site repo not found at {site}")
    slug = slug_for(login)
    folder = os.path.join(site, "samples", slug)
    os.makedirs(folder, exist_ok=True)
    clip = os.path.join(folder, "clip.mp4")
    size = transcode(sample, clip, run=run)
    if size > MAX_BYTES:
        print(f"warning: {size/1e6:.1f} MB after CRF 34, over the 6 MB target", file=sys.stderr)
    with open(os.path.join(folder, "index.html"), "w") as f:
        f.write(page_html(login, title, views, when))
    git(site, "add", f"samples/{slug}", run=run)
    git(site, "commit", "-q", "-m", f"samples: private page for {login}", run=run)
    if push:
        git(site, "push", "origin", "main", run=run)
    return BASE_URL + slug + "/"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sample"); ap.add_argument("login")
    ap.add_argument("--title", default=""); ap.add_argument("--views", type=int, default=0)
    ap.add_argument("--when", default="", help="e.g. 'Tuesday' or 'Sept 23'")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--no-push", action="store_true", help="commit locally only (the push publishes)")
    a = ap.parse_args(argv)
    url = publish(a.sample, a.login, title=a.title, views=a.views, when=a.when, site=a.site, push=not a.no_push)
    print(url + ("" if not a.no_push else "   (committed, NOT pushed: not live until `git push origin main` in the site repo)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
