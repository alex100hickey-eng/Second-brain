"""clipbot — the clipping system: campaign footage in, ready-to-post transformed clips out.

Flow:  inbox (iCloud Drive) or URL → OpusClip API cuts clips → HD download → ffmpeg adds Alex's
       recorded hook line + an on-screen text hook + a distinct trim per platform → staged into
       iCloud Drive `ClipBot/ready/<platform>/` with a caption file beside each → one nudge a day.
       Alex posts from his phone when the window is right (the "draft" he asked for). An optional
       second driver schedules OpusClip's own clip through its social poster instead.

Why the transform step exists: caption-only OpusClip output is classed "unoriginal" on TikTok
(Dec 2025 policy), Instagram (Apr 2026) and YouTube, which kills For You reach. A voice hook and
a re-trim are the cheapest edits that pass. Design: vault `Money/Side Hustles — Clipping +
Polymarket (2026-09-11).md` §1–4.
"""

__version__ = "0.1.0"
