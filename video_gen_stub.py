"""
AI Video GENERATION — V2 STUB (not implemented tonight)
=======================================================

This is a DELIBERATE STUB. Text-to-video / image-to-video *generation* is out of scope for
the v1 toolkit (which does real editing via ffmpeg in `video_toolkit.py`). This file documents
exactly what a V2 needs so it can slot into the existing Viewmax flow later, and gives the
editing toolkit a clean interface to call once a provider + key exist.

Nothing here makes network calls. Every function raises NotImplementedError with guidance.

------------------------------------------------------------------------------------------
WHAT A V2 NEEDS
------------------------------------------------------------------------------------------
1. A provider + API key (pick one, drop the key in the project `.env`):
   - Runway Gen-3 / Gen-4      → RUNWAY_API_KEY        (text+image→video, strong motion)
   - Luma Dream Machine        → LUMA_API_KEY          (good value, image→video)
   - Google Veo (via Vertex)   → GOOGLE_* / Vertex creds (highest quality, gated access)
   - Pika                      → PIKA_API_KEY
   - Kling / Minimax           → KLING_API_KEY / MINIMAX_API_KEY (cheap, strong)
   - Stability (SVD, self-host) → STABILITY_API_KEY or local GPU (open weights, no per-clip cost)

   All are async "job" APIs: POST a prompt → get a job id → poll until done → download an mp4.
   So the interface below is submit → poll → fetch. Wire `generate_clip()` to that pattern.

2. Config knobs the Viewmax flow will want to pass through:
   prompt, optional init_image, duration_seconds, aspect_ratio (default "9:16" for Shorts),
   fps, seed, motion/camera hints, and a per-run cost cap.

3. Cost + safety: generation costs real money per second of output — so per the project's
   hard rules it MUST route through the dashboard approval queue before spending, exactly like
   calendar/file actions do. Do NOT let an autonomous agent call a paid generator unattended.

------------------------------------------------------------------------------------------
HOW IT SLOTS INTO THE EXISTING FLOW
------------------------------------------------------------------------------------------
- money_clips_agent.py already produces the CONCEPT (topic, hook, script, captions).
- A V2 would: take that concept → generate B-roll clips with `generate_clip()` per scene →
  hand the clips to `video_toolkit.concat()` / `caption()` / `to_vertical()` to assemble a
  finished 9:16 Short → save to media_lib/ and log to "Agent Outputs" for review.
- So V2 = this generator + the v1 editing toolkit that already exists. The seam is clean:
  generation returns file paths; the toolkit takes file paths.
"""

import os

# Where a provider key WOULD be read from (none required today).
PROVIDER_ENV_KEYS = [
    "RUNWAY_API_KEY", "LUMA_API_KEY", "PIKA_API_KEY",
    "KLING_API_KEY", "MINIMAX_API_KEY", "STABILITY_API_KEY",
]


def available_provider() -> str | None:
    """Return the first configured provider key name, or None. Lets callers detect whether
    generation is wired without importing anything provider-specific."""
    for k in PROVIDER_ENV_KEYS:
        if os.environ.get(k):
            return k
    return None


def generate_clip(prompt: str, init_image: str = None, duration_seconds: float = 5.0,
                  aspect_ratio: str = "9:16", fps: int = 24, seed: int = None,
                  cost_cap_usd: float = None) -> str:
    """[V2] Generate a single AI video clip from a text (and optional image) prompt.

    Intended contract (implement against your chosen provider):
      submit job → poll status → download mp4 to media_lib/ → return its path.

    Raises NotImplementedError today. To implement:
      1. Set a provider key in .env (see PROVIDER_ENV_KEYS).
      2. Replace the body with: POST prompt to the provider, poll the job id, download result.
      3. Route the spend through the dashboard approval queue BEFORE calling the paid API.
    """
    provider = available_provider()
    hint = (f"A provider key is set ({provider}), but the generation call isn't wired yet — "
            f"implement generate_clip() against that provider's submit/poll/fetch API."
            if provider else
            "No video-generation provider key found in .env. Add one of "
            f"{', '.join(PROVIDER_ENV_KEYS)} and implement generate_clip().")
    raise NotImplementedError(
        "AI video generation is a V2 feature and is intentionally not implemented. " + hint
    )


def assemble_short_from_concept(concept: dict) -> str:
    """[V2] Turn a money_clips_agent concept (topic/hook/script/captions) into a finished
    9:16 Short: generate B-roll per scene, then use video_toolkit to concat + caption +
    verticalize. Stubbed until generate_clip() is implemented."""
    raise NotImplementedError(
        "assemble_short_from_concept is V2. Once generate_clip() works, generate per-scene "
        "clips then call video_toolkit.concat/caption/to_vertical to assemble the Short."
    )


if __name__ == "__main__":
    print(__doc__)
    p = available_provider()
    print("Configured provider:", p or "none (V2 not wired — this is expected)")
