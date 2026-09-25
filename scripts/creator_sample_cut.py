#!/usr/bin/env python3
"""creator_sample_cut.py — a vertical sample clip with burned captions, for the creator lane's S1→S2 step.

    python3 scripts/creator_sample_cut.py <clip-url-or-file> <out.mp4> [--start S] [--end S] [--srt captions.srt]

Downloads a Twitch/Kick clip with yt-dlp (or takes a local file), transcribes with whisper-cli into an SRT
(or uses the one given, hand-corrected), center-crops 16:9 to 9:16 at 1080x1920 and burns one caption per
SRT cue. Homebrew ffmpeg has no `drawtext`/`subtitles`, so every cue is a Pillow-rendered PNG overlaid with
`enable=between(t,a,b)` — the same trick clipbot's transform.py uses for its hook card. Proven 2026-09-24
on AnthonyZ's studio clip. Never writes under ClipBot/ready (clipbot posts from there).
"""
from __future__ import annotations
import argparse, os, re, shutil, subprocess, sys, tempfile

FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
MODEL = os.path.expanduser("~/second-brain/models/ggml-base.en.bin")
W, H = 1080, 1920
BIN = "/opt/homebrew/bin"


def t2s(t: str) -> float:
    h, m, s = t.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(text: str) -> list:
    """[(start_s, end_s, text)] with music marks stripped and empty cues dropped. Pure."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.strip().splitlines()]
        if len(lines) < 3:
            continue
        times = re.findall(r"\d+:\d+:\d+[,.]\d+", lines[1])
        if len(times) < 2:
            continue
        body = " ".join(l.strip() for l in lines[2:]).replace("♪", "").strip()
        # whisper's silence/music markers are not captions
        if re.fullmatch(r"(\[[A-Z _]+\]\s*)+", body):
            body = ""
        if body:
            out.append((t2s(times[0]), t2s(times[1]), body))
    return out


def wrap(text: str, measure, maxw: int, max_lines: int = 3) -> list:
    """Greedy word wrap against a width function. Pure; `measure(str) -> px`."""
    lines, cur = [], ""
    for w_ in text.split():
        t = (cur + " " + w_).strip()
        if measure(t) > maxw and cur:
            lines.append(cur)
            cur = w_
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def build_filter(cues: list, heights: list, start: float, end: float | None) -> str:
    """The ffmpeg filter_complex for crop + one overlay per cue. Pure, so it is testable."""
    fc = f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={W}:{H},fps=30[v0]"
    prev = "v0"
    for i, ((a, b, _t), h) in enumerate(zip(cues, heights), start=1):
        y = H - 420 - h
        fc += f";[{prev}][{i}:v]overlay=0:{y}:enable='between(t,{max(a - start, 0):.2f},{max(b - start, 0):.2f})'[v{i}]"
        prev = f"v{i}"
    return fc + f";[{prev}]null[vout]"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("out")
    ap.add_argument("--start", type=float, default=0.0); ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--srt", default="")
    a = ap.parse_args(argv)
    if "/ready/" in os.path.abspath(a.out).replace("\\", "/"):
        print("refusing to write under a ClipBot ready folder"); return 2
    from PIL import Image, ImageDraw, ImageFont  # lazy: the pure helpers need no Pillow
    tmp = tempfile.mkdtemp(prefix="creator_cut_")
    try:
        return _run(a, tmp, Image, ImageDraw, ImageFont)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # a 50 MB source per cut must not pile up in /var/folders


def _run(a, tmp, Image, ImageDraw, ImageFont) -> int:
    src = a.source
    if src.startswith("http"):
        subprocess.run([f"{BIN}/yt-dlp", "-q", "--no-warnings", "-f", "best", "-o", os.path.join(tmp, "src.%(ext)s"), src], check=True)
        src = next(os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("src."))
    srt = a.srt
    if not srt:
        wav = os.path.join(tmp, "audio.wav")
        subprocess.run([f"{BIN}/ffmpeg", "-v", "error", "-y", "-i", src, "-ac", "1", "-ar", "16000", wav], check=True)
        subprocess.run([f"{BIN}/whisper-cli", "-m", MODEL, "-f", wav, "-osrt", "-of", os.path.join(tmp, "captions"), "-np"],
                       check=True, capture_output=True)
        srt = os.path.join(tmp, "captions.srt")
    # keep the caption file beside the output: whisper mishears names, and a corrected SRT re-run
    # (--srt) is the fix; a caption file that vanished with the temp dir cannot be corrected
    kept = os.path.splitext(a.out)[0] + ".srt"
    if os.path.abspath(srt) != os.path.abspath(kept):
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        shutil.copyfile(srt, kept)
    cues = [c for c in parse_srt(open(srt, encoding="utf-8").read())
            if c[1] > a.start and (a.end is None or c[0] < a.end)]
    font = ImageFont.truetype(FONT, 58)
    meas = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    inputs, heights = ["-i", src], []
    for i, (_a, _b, text) in enumerate(cues):
        lines = wrap(text, lambda s: meas.textbbox((0, 0), s, font=font)[2], W - 160)
        lh = 72; h = lh * len(lines) + 40
        img = Image.new("RGBA", (W, h), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
        for j, ln in enumerate(lines):
            bb = d.textbbox((0, 0), ln, font=font, stroke_width=6)
            d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], 20 + j * lh - bb[1]), ln, font=font, fill="white",
                   stroke_width=6, stroke_fill="black")
        p = os.path.join(tmp, f"c{i:03d}.png"); img.save(p); inputs += ["-i", p]; heights.append(h)
    fc = build_filter(cues, heights, a.start, a.end)
    cmd = [f"{BIN}/ffmpeg", "-v", "error", "-y"]
    if a.start: cmd += ["-ss", str(a.start)]
    if a.end is not None: cmd += ["-to", str(a.end)]
    cmd += inputs + ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", a.out]
    subprocess.run(cmd, check=True)
    print(f"{len(cues)} captions -> {a.out} ({os.path.getsize(a.out) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
