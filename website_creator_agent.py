"""
Website Creator Agent — Second Brain

Takes a written BRIEF (site purpose, pages, style notes) and produces a complete,
previewable static website in `sites/<slug>/` — real HTML/CSS/JS, a coherent design
system, responsive layout, no lorem-ipsum, no generic-AI aesthetic.

It works in staged passes (a single agent doing multiple focused Claude calls):
  1. PLAN     — turn the brief into a concrete site plan + design tokens (JSON).
  2. STYLES   — generate one polished, coherent stylesheet from those tokens.
  3. PAGES    — build each page with real copy, using the shared component classes.
  4. REVIEW   — a self-review polish pass that hardens the stylesheet for consistency
                and responsiveness.

Then it writes the files, a per-site README, and a one-command `serve.sh` preview
(python http.server on port 8080 by default — NOT 5001, the chat app's port).
Nothing is deployed. Logs a summary row to Supabase "Agent Outputs".

Run standalone:
    python3 website_creator_agent.py --brief "A landing site for a local sourdough bakery..."
    python3 website_creator_agent.py --brief-file brief.txt
    python3 website_creator_agent.py --brief "..." --port 8090

Preview a generated site:
    bash sites/<slug>/serve.sh      # then open http://localhost:8080
"""

import os
import re
import sys
import json
import time
import threading
import argparse
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from anthropic import Anthropic
from supabase import create_client

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

AGENT_NAME = "website_creator_agent"
MODEL = "claude-sonnet-5"
SITES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites")
DEFAULT_PREVIEW_PORT = 8080


# ============================================================
# DESIGN SYSTEM — the "skill" (baked into every stage's system prompt)
# The tutorial's frontend-design skill, adapted for an autonomous static-HTML
# generator: taste rules the model must follow on every call.
# ============================================================
DESIGN_SYSTEM = """DESIGN SYSTEM — follow on every decision:
- TYPE SCALE: use a real modular scale, not random sizes. Fluid headings via clamp()
  (e.g. h1 clamp(2.5rem,6vw,4.5rem)), a clear h1>h2>h3>body>small hierarchy, generous
  line-height on body (1.6-1.75), tighter on headings (1.05-1.2). One heading font + one
  body font, both characterful (never Roboto/Open Sans/Arial defaults).
- SPACING: an 8px base grid. Section vertical rhythm is large and deliberate
  (clamp(4rem,10vw,8rem) block padding). Whitespace is a feature, not a gap to fill.
- COLOR: use ONLY the design tokens (CSS variables) — no stray hex codes in components.
  Real contrast, one confident accent, restrained neutrals. Accessible text/bg contrast.
- MOTION: the site should feel alive — scroll-reveal entrances, staggered card reveals,
  smooth hover transitions. (The build appends a motion layer + observer automatically;
  your job is to add the data-reveal hooks and tasteful :hover states.)
- POLISH: real focus-visible rings, rounded corners via var(--radius), soft shadows via
  var(--shadow), hover lift on cards/buttons, consistent border treatment.
- ANTI-GENERIC: avoid the generic AI look — everything centered, three identical grey
  cards, one hero and nothing else, purple gradient on white. Aim for an intentional,
  editorial, agency-grade result with a distinctive point of view."""

# ============================================================
# SECTION BLUEPRINTS — the "21st.dev" equivalent: a curated set of premium
# section patterns (as structure/guidance, since we emit static HTML not React).
# ============================================================
SECTION_BLUEPRINTS = """PREMIUM SECTION PATTERNS to draw from (pick what fits the brief; adapt, don't dump all):
- STICKY NAV: brand left, links right, mobile hamburger; subtle shadow/blur on scroll.
- HERO: strong headline + one-line subhead + primary & secondary CTA; a supporting visual
  block (use a .media-ph placeholder or inline SVG — never an external image file).
- FEATURE GRID: 3-4 .card items in a .card-grid, each with an icon (inline SVG/emoji),
  a short title, and a concrete benefit line. Stagger their reveal (data-reveal-group).
- SOCIAL PROOF: testimonial cards or a stat row (big number + label).
- PRICING: 2-3 tiers, one highlighted "featured" plan, clear CTA per tier.
- FAQ: an accordion (.faq with <button class="faq-q"> toggling .faq-a) — JS is provided.
- CTA BAND: a full-width closing call-to-action with a single clear action.
- FOOTER: multi-column .site-footer with brand, links, and copyright."""

# ============================================================
# MOTION LAYER — appended to EVERY stylesheet so scroll-reveal + hover polish is
# guaranteed and consistent regardless of what the CSS model produced. This is the
# vanilla equivalent of Framer Motion's scroll reveals / staggered entrances.
# Progressive enhancement: only hides content once main.js flags .reveal-ready, and
# fully disabled under prefers-reduced-motion.
# ============================================================
MOTION_CSS = """
/* ===== motion layer (auto-appended) — scroll reveals, stagger, hover polish ===== */
[data-reveal], [data-reveal-group] > * {
  transition: opacity .7s cubic-bezier(.16,1,.3,1), transform .7s cubic-bezier(.16,1,.3,1);
}
html.reveal-ready [data-reveal],
html.reveal-ready [data-reveal-group] > * {
  opacity: 0;
  transform: translateY(24px);
}
html.reveal-ready [data-reveal].in-view,
html.reveal-ready [data-reveal-group].in-view > * {
  opacity: 1;
  transform: none;
}
/* stagger children of a revealed group */
[data-reveal-group].in-view > *:nth-child(1) { transition-delay: .00s; }
[data-reveal-group].in-view > *:nth-child(2) { transition-delay: .08s; }
[data-reveal-group].in-view > *:nth-child(3) { transition-delay: .16s; }
[data-reveal-group].in-view > *:nth-child(4) { transition-delay: .24s; }
[data-reveal-group].in-view > *:nth-child(5) { transition-delay: .32s; }
[data-reveal-group].in-view > *:nth-child(6) { transition-delay: .40s; }

/* hover polish */
.card { transition: transform .25s ease, box-shadow .25s ease; }
.card:hover { transform: translateY(-4px); }
.btn, .btn-primary { transition: transform .18s ease, box-shadow .18s ease, background-color .18s ease, color .18s ease; }
.btn:hover, .btn-primary:hover { transform: translateY(-2px); }

/* generated-image placeholder — replaces any missing <img> so nothing renders broken */
.media-ph {
  display: flex; align-items: center; justify-content: center; text-align: center;
  aspect-ratio: 16 / 9; width: 100%; overflow: hidden;
  border-radius: var(--radius, 12px);
  color: var(--on-primary, #fff);
  background:
    radial-gradient(120% 120% at 15% 15%, color-mix(in srgb, var(--accent, #6c8cff) 55%, transparent), transparent 60%),
    linear-gradient(135deg, var(--primary, #3a3a55), color-mix(in srgb, var(--primary, #3a3a55) 60%, #000));
  font: 500 .85rem/1.3 system-ui, sans-serif; letter-spacing: .02em; padding: 1rem;
}
.media-ph[data-shape="square"] { aspect-ratio: 1 / 1; }
.media-ph[data-shape="portrait"] { aspect-ratio: 3 / 4; }
.media-ph .media-ph__label { opacity: .85; max-width: 22ch; }

/* FAQ accordion */
.faq-q { cursor: pointer; width: 100%; text-align: left; display: flex;
  justify-content: space-between; gap: 1rem; align-items: center; }
.faq-a { overflow: hidden; max-height: 0; transition: max-height .3s ease; }
.faq-q[aria-expanded="true"] + .faq-a { max-height: 40rem; }

@media (prefers-reduced-motion: reduce) {
  html.reveal-ready [data-reveal],
  html.reveal-ready [data-reveal-group] > * { opacity: 1 !important; transform: none !important; }
  .card:hover, .btn:hover, .btn-primary:hover { transform: none; }
  [data-reveal], [data-reveal-group] > *, .card, .btn, .btn-primary, .faq-a { transition: none; }
}
"""

# ============================================================
# CINEMATIC SCROLL INTRO — deterministic scroll-scrubbed "travel-in" hero.
# A stack of pinned full-screen scenes; each scene's background zooms and its
# giant headline enters/exits as you scroll through its track, then the page
# releases into the normal content ("the payoff"). The scroll mechanics live
# here (guaranteed correct) — the model only writes the scene copy.
# Each scene's visual is a CSS gradient PLACEHOLDER by default; drop in a real
# photo/video by setting --scene-img: url('yourphoto.jpg') on .cinema__bg.
# ============================================================
CINEMATIC_CSS = """
/* ===== cinematic scroll intro (auto-appended when enabled) ===== */
.cinema { position: relative; background: #05070d; }
.cinema__scene { position: relative; height: var(--scene-len, 190vh); }
.cinema__stage {
  position: sticky; top: 0; height: 100vh; overflow: hidden;
  display: grid; place-items: center; isolation: isolate;
}
.cinema__bg {
  position: absolute; inset: -2%; z-index: -2;
  background-image: var(--scene-img, var(--scene-grad, linear-gradient(160deg, var(--primary, #222), #000)));
  background-size: cover; background-position: center;
  transform: scale(calc(1 + (var(--p, 0) * 0.30)));
  will-change: transform;
}
.cinema__stage::after {  /* legibility scrim */
  content: ""; position: absolute; inset: 0; z-index: -1;
  background: radial-gradient(130% 100% at 50% 55%, transparent 28%, rgba(0,0,0,.55) 100%),
              linear-gradient(180deg, rgba(0,0,0,.45), rgba(0,0,0,.10) 45%, rgba(0,0,0,.65));
}
.cinema__content {
  text-align: center; color: #fff; padding: 2rem; max-width: min(92vw, 42rem);
  opacity: var(--enter, 1);
  transform: translateY(calc((1 - var(--enter, 1)) * 42px)) scale(calc(0.98 + var(--enter,1) * 0.02));
  will-change: opacity, transform;
}
.cinema__eyebrow {
  text-transform: uppercase; letter-spacing: .38em; font-size: .72rem;
  opacity: .85; margin: 0 0 1rem;
}
.cinema__title {
  font-size: clamp(2.5rem, 8vw, 6rem); line-height: .95; font-weight: 800;
  text-transform: uppercase; letter-spacing: -.015em; margin: 0;
  text-shadow: 0 6px 48px rgba(0,0,0,.55);
}
.cinema__caption {
  margin: 1.25rem auto 0; font-size: clamp(1rem, 1.8vw, 1.3rem);
  line-height: 1.5; opacity: .92; max-width: 42ch;
}
.cinema__cue {
  position: sticky; bottom: 1.75rem; z-index: 3; display: block; width: max-content;
  margin: -6rem auto 0; padding: .55rem .9rem; color: #fff; text-decoration: none;
  font-size: .72rem; letter-spacing: .25em; text-transform: uppercase; opacity: .8;
  border: 1px solid rgba(255,255,255,.35); border-radius: 999px;
  animation: cinemaCue 2s ease-in-out infinite;
}
@keyframes cinemaCue { 0%,100%{ transform: translateY(0); opacity:.55 } 50%{ transform: translateY(5px); opacity:.95 } }
.cinema + * { position: relative; z-index: 2; }  /* content sits above the cinema backdrop */

@media (prefers-reduced-motion: reduce) {
  .cinema__scene { height: 100vh; }
  .cinema__bg { transform: none !important; }
  .cinema__content { opacity: 1 !important; transform: none !important; }
  .cinema__cue { animation: none; }
}
"""

CINEMATIC_JS = """
// Cinematic scroll scrubber — drives each pinned scene's zoom + headline enter/exit.
(function () {
  var cinema = document.querySelector('[data-cinema]');
  if (!cinema) return;
  var scenes = Array.prototype.slice.call(cinema.querySelectorAll('[data-scene]'));
  if (!scenes.length) return;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) { scenes.forEach(function (s) { s.style.setProperty('--p', 0); s.style.setProperty('--enter', 1); }); return; }
  var clamp = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };
  var ticking = false;
  function update() {
    ticking = false;
    var vh = window.innerHeight;
    for (var i = 0; i < scenes.length; i++) {
      var track = scenes[i];
      var r = track.getBoundingClientRect();
      var span = Math.max(1, track.offsetHeight - vh);
      var p = clamp(-r.top / span);                 // 0 -> 1 across the scene's scroll track
      track.style.setProperty('--p', p.toFixed(4));
      var enter = 1;                                 // headline eases in, holds, eases out
      if (p < 0.16) enter = p / 0.16;
      else if (p > 0.80) enter = clamp((1 - p) / 0.20);
      track.style.setProperty('--enter', enter.toFixed(4));
    }
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  update();
})();
"""


# ---- small helpers ----
def _extract_json(text: str) -> dict:
    """Parse JSON from a model reply, tolerating markdown fences / stray prose."""
    t = text.strip().replace("```json", "```")
    if "```" in t:
        parts = t.split("```")
        # take the longest fenced block
        t = max(parts, key=len)
    # find the outermost {...}
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1:
        t = t[start:end + 1]
    return json.loads(t)


def _strip_code_fence(text: str, lang: str = "") -> str:
    """Return raw code from a reply that may be wrapped in ``` fences. Robust to fences
    anywhere and to trailing whitespace/newlines after the closing fence."""
    # Drop any line that is just a fence marker (``` or ```css etc.), wherever it appears.
    lines = [ln for ln in text.splitlines() if not re.match(r"^\s*```[a-zA-Z]*\s*$", ln)]
    return "\n".join(lines).strip()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "site")[:50]


def _call(claude, system, user, max_tokens=4096):
    msg = claude.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _call_json(claude, system, user, schema, tool_name="emit", max_tokens=2000):
    """Force the model to return structured JSON via a single required tool call.
    Guarantees valid JSON — no fragile text parsing / quote-escaping issues."""
    msg = claude.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        tools=[{"name": tool_name, "description": "Return the structured result.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user}],
    )
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    raise RuntimeError("Model returned no structured output.")


# ============================================================
# STAGE 1 — PLAN + DESIGN TOKENS
# ============================================================
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "slug": {"type": "string", "description": "url-safe short slug"},
        "tagline": {"type": "string"},
        "audience": {"type": "string"},
        "tone": {"type": "string", "description": "voice in 3-5 words"},
        "pages": {
            "type": "array",
            "description": "3-5 pages that genuinely fit the brief; Home first.",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "e.g. index.html"},
                    "nav_label": {"type": "string"},
                    "title": {"type": "string", "description": "<title> text"},
                    "purpose": {"type": "string"},
                    "sections": {"type": "array", "items": {"type": "string"},
                                 "description": "specific section names for THIS site's content"},
                },
                "required": ["filename", "nav_label", "title", "purpose", "sections"],
            },
        },
        "design": {
            "type": "object",
            "properties": {
                "aesthetic": {"type": "string",
                              "description": "2-3 sentences: a specific, non-generic visual direction"},
                "fonts": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "description": "a real Google Font family"},
                        "body": {"type": "string", "description": "a real Google Font family"},
                        "google_fonts_href": {"type": "string",
                                              "description": "https://fonts.googleapis.com/css2?family=...&display=swap"},
                    },
                    "required": ["heading", "body", "google_fonts_href"],
                },
                "colors": {
                    "type": "object",
                    "properties": {k: {"type": "string"} for k in
                                   ["bg", "surface", "text", "muted", "primary", "accent", "border", "on_primary"]},
                    "required": ["bg", "surface", "text", "muted", "primary", "accent", "border", "on_primary"],
                },
                "radius": {"type": "string"},
                "shadow": {"type": "string"},
                "vibe_keywords": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["aesthetic", "fonts", "colors", "radius", "shadow", "vibe_keywords"],
        },
    },
    "required": ["name", "slug", "tagline", "audience", "tone", "pages", "design"],
}


def plan_site(claude, brief: str) -> dict:
    system = (
        "You are a senior web designer + information architect. Given a brief, you produce a "
        "concrete, buildable plan for a small static website with a DISTINCTIVE, intentional "
        "visual identity — never a generic AI template.\n\n" + DESIGN_SYSTEM + "\n\n" + SECTION_BLUEPRINTS
    )
    user = f"""BRIEF:
{brief}

Produce the site plan by calling the tool. Rules:
- Choose 3-5 pages that genuinely fit the brief (Home always first; add e.g. About, Menu,
  Services, Membership, Contact as relevant).
- Pick a REAL, characterful Google Font pairing that matches the brand — not defaults like
  Roboto/Open Sans. Build a correct google_fonts_href for BOTH families.
- Pick a considered color palette with genuine contrast and personality (dark or light —
  whatever fits). Ensure text/bg contrast is accessible.
- Section names must be specific to this site's content, not generic placeholders."""
    plan = _call_json(claude, system, user, PLAN_SCHEMA, tool_name="emit_site_plan", max_tokens=2000)
    plan["slug"] = _slugify(plan.get("slug") or plan.get("name") or "site")
    if not plan.get("pages"):
        plan["pages"] = [{"filename": "index.html", "nav_label": "Home",
                          "title": plan.get("name", "Home"), "purpose": "landing",
                          "sections": ["hero", "about", "contact"]}]
    # ensure filenames end in .html
    for p in plan["pages"]:
        if not p.get("filename", "").endswith(".html"):
            p["filename"] = _slugify(p.get("nav_label", "page")) + ".html"
    return plan


# ============================================================
# STAGE 1b — CINEMATIC INTRO SCENES (only when cinematic mode is on)
# ============================================================
SCENES_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "description": "5-6 cinematic intro beats that build a sense of arrival.",
            "items": {
                "type": "object",
                "properties": {
                    "eyebrow": {"type": "string", "description": "tiny uppercase kicker, 1-3 words"},
                    "title": {"type": "string", "description": "huge punchy headline, 2-4 words"},
                    "caption": {"type": "string", "description": "one evocative sentence"},
                    "visual": {"type": "string",
                               "description": "the ideal photo/video for this beat (used as the placeholder label + a hint for swapping in real footage)"},
                },
                "required": ["title", "caption", "visual"],
            },
        }
    },
    "required": ["scenes"],
}


def plan_cinema_scenes(claude, plan: dict, brief: str) -> list:
    system = (
        "You are a creative director scripting a cinematic, scroll-scrubbed website intro — the "
        "immersive 'travel-in' sequence a visitor scrolls through BEFORE reaching the practical "
        "content. Big, punchy, emotional, on-brand. Every word earns its place."
    )
    user = f"""BRIEF:
{brief}

Brand: {plan.get('name')} — {plan.get('tagline')}
Aesthetic: {plan['design'].get('aesthetic')}

Write 5-6 cinematic intro SCENES that build a sense of ARRIVAL: start wide (the world/city/context),
then move closer and INSIDE this place or experience, then land on an emotional payoff that makes the
visitor want to act. For each scene give:
- eyebrow: a tiny uppercase kicker (1-3 words)
- title: a HUGE punchy headline, 2-4 words (looks great in all-caps)
- caption: one evocative sentence
- visual: the ideal photo or video footage for that beat (concrete and specific)
The last scene should feel like a threshold — "step in / take your seat / let's go".
Return via the tool."""
    data = _call_json(claude, system, user, SCENES_SCHEMA, tool_name="emit_scenes", max_tokens=1500)
    scenes = data.get("scenes", []) or []
    return scenes[:7]


def _render_cinema(scenes: list, colors: dict) -> str:
    """Assemble the cinematic intro HTML deterministically from scene copy. The scroll
    mechanics come entirely from CINEMATIC_CSS/JS; here we only place the copy + a per-scene
    gradient placeholder (swappable for a real image via --scene-img)."""
    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    c = colors or {}
    primary = c.get("primary", "#1e2a4a")
    accent = c.get("accent", "#45d6ff")
    surface = c.get("surface", "#111826")
    # gradient recipes that progress from distant/cool toward arrived/warm+bright
    recipes = [
        f"linear-gradient(165deg, {surface}, #05070d 85%)",
        f"radial-gradient(130% 120% at 50% 15%, {primary}, #05070d 72%)",
        f"linear-gradient(180deg, {primary}, #05070d)",
        f"radial-gradient(120% 110% at 50% 55%, {accent}, {primary} 58%, #05070d)",
        f"linear-gradient(200deg, {accent}, {primary} 55%, #05070d)",
        f"radial-gradient(130% 130% at 50% 60%, {accent}, {primary} 45%, #000)",
    ]
    parts = ['<section class="cinema" data-cinema>']
    n = len(scenes)
    for i, s in enumerate(scenes):
        grad = recipes[min(i, len(recipes) - 1)] if n <= len(recipes) else recipes[i % len(recipes)]
        eyebrow = f'<p class="cinema__eyebrow">{esc(s.get("eyebrow"))}</p>' if s.get("eyebrow") else ""
        caption = f'<p class="cinema__caption">{esc(s.get("caption"))}</p>' if s.get("caption") else ""
        # visual description travels as a comment + aria so it's obvious where real footage goes
        parts.append(
            f'  <div class="cinema__scene" data-scene>\n'
            f'    <div class="cinema__stage">\n'
            f'      <!-- swap placeholder for real footage: set --scene-img: url(\'photoN.jpg\') below. '
            f'Ideal shot: {esc(s.get("visual"))} -->\n'
            f'      <div class="cinema__bg" style="--scene-grad: {grad};" role="img" '
            f'aria-label="{esc(s.get("visual"))}"></div>\n'
            f'      <div class="cinema__content">\n'
            f'        {eyebrow}\n'
            f'        <h2 class="cinema__title">{esc(s.get("title"))}</h2>\n'
            f'        {caption}\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </div>'
        )
    parts.append('  <a class="cinema__cue" href="#start">Scroll</a>')
    parts.append('</section>\n<span id="start" aria-hidden="true"></span>')
    return "\n".join(parts)


def _inject_cinema(html: str, cinema_html: str) -> str:
    """Insert the cinematic intro right after the site nav (so the sticky nav stays on top),
    falling back to just after <body>."""
    m = re.search(r'</nav>', html, re.I)
    if m:
        i = m.end()
        return html[:i] + "\n" + cinema_html + "\n" + html[i:]
    m = re.search(r'<body[^>]*>', html, re.I)
    if m:
        i = m.end()
        return html[:i] + "\n" + cinema_html + "\n" + html[i:]
    return cinema_html + "\n" + html


# ============================================================
# STAGE 2 — STYLESHEET
# ============================================================
def build_stylesheet(claude, plan: dict) -> tuple:
    d = plan["design"]
    nav = " · ".join(p.get("nav_label", "") for p in plan["pages"])
    system = (
        "You are a front-end engineer with strong visual taste. You write clean, modern, "
        "hand-crafted CSS that reads as intentional design — not a bootstrap-looking template. "
        "You use CSS custom properties, fluid type (clamp), flex/grid, and tasteful detail "
        "(considered spacing, hover states, subtle motion). Mobile-first and fully responsive.\n\n"
        + DESIGN_SYSTEM
    )
    user = f"""Build ONE stylesheet `styles.css` for this site.

Brand: {plan.get('name')} — {plan.get('tagline')}
Aesthetic: {d.get('aesthetic')}
Vibe: {', '.join(d.get('vibe_keywords', []))}
Nav pages: {nav}

Design tokens (define these as :root CSS variables and USE them consistently):
{json.dumps(d.get('colors', {}), indent=2)}
radius: {d.get('radius')}
shadow: {d.get('shadow')}
Heading font: "{d['fonts']['heading']}", Body font: "{d['fonts']['body']}" (loaded via <link> in the HTML; just reference them).

Requirements:
- Style these components so the pages can rely on them: a sticky top .site-nav with brand + links + a
  mobile hamburger (.nav-toggle) that shows/hides .nav-links; .btn and .btn-primary buttons;
  .hero; .container (max-width wrapper); .card / .card-grid; .section with generous vertical rhythm;
  a .site-footer; form fields. Add a .nav-links.open rule for the mobile menu.
- Also style these premium-pattern classes the pages may use: .stat / .stat-num, .testimonial,
  .pricing-grid / .price-card / .price-card.featured (visually elevate the featured tier),
  .faq / .faq-q / .faq-a, and a full-width .cta-band closing section.
- Fluid, responsive, accessible contrast, visible :focus-visible states, prefers-reduced-motion respect.
- Give it real personality consistent with the aesthetic. NO generic centered-everything AI look.
- NOTE: a motion layer (scroll-reveal, staggered entrances, hover lift, a .media-ph image placeholder,
  and FAQ accordion behavior) is AUTOMATICALLY appended after your CSS — do NOT fight it. You may add
  your own tasteful :hover states, but do not redefine .media-ph or the reveal/opacity behavior.

Return ONLY the CSS (no markdown, no explanation). Then on a NEW LINE after the CSS, output a line
starting with `CLASS_REFERENCE:` followed by a compact comma-separated list of the main class names you
defined, so page-builders reuse them exactly."""
    raw = _call(claude, system, user, max_tokens=4096)
    # split off the class reference line if present
    class_ref = ""
    if "CLASS_REFERENCE:" in raw:
        css_part, ref_part = raw.rsplit("CLASS_REFERENCE:", 1)
        class_ref = ref_part.strip().strip("`").strip()
        raw = css_part
    css = _strip_code_fence(raw, "css")
    return css, class_ref


# ============================================================
# STAGE 3 — PAGES
# ============================================================
def build_page(claude, plan: dict, page: dict, class_ref: str, cinematic_home: bool = False) -> str:
    d = plan["design"]
    nav_items = "".join(
        f'<a href="{p["filename"]}">{p.get("nav_label", "")}</a>' for p in plan["pages"]
    )
    other_pages = ", ".join(f'{p["nav_label"]} ({p["filename"]})' for p in plan["pages"])
    system = (
        "You are a web developer AND a sharp copywriter. You write complete, semantic, accessible "
        "HTML5 pages with REAL, specific, on-brand copy — never lorem ipsum, never vague filler "
        "like 'we provide quality solutions'. Every headline and paragraph is concrete and useful.\n\n"
        + DESIGN_SYSTEM + "\n\n" + SECTION_BLUEPRINTS
    )
    cinema_note = ""
    if cinematic_home:
        cinema_note = (
            "\n\nIMPORTANT — CINEMATIC INTRO: a full-screen cinematic scroll intro is inserted "
            "automatically at the very top of this page (right after the nav). So do NOT write your "
            "own big hero/headline section here. Begin the page body directly with the PRACTICAL "
            "PAYOFF content — the sections a visitor wants after the immersive intro (e.g. services & "
            "pricing, social proof, an FAQ, a visit/location + hours block, and a strong final CTA). "
            "Treat this page as 'the details after the movie'."
        )
    user = f"""Write the COMPLETE HTML for `{page['filename']}` of this site.

Site: {plan.get('name')} — {plan.get('tagline')}
Audience: {plan.get('audience')} · Tone: {plan.get('tone')}
This page's purpose: {page.get('purpose')}
Sections to include (in order, with real content): {', '.join(page.get('sections', []))}
Other pages (for nav + internal links): {other_pages}{cinema_note}

Hard requirements:
- Full document: <!DOCTYPE html>, <html lang="en">, <head> with charset, viewport, a good <title>
  and <meta name="description">.
- In <head>, link the fonts: <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="{d['fonts']['google_fonts_href']}">
  and <link rel="stylesheet" href="styles.css">. Before </body> add <script src="main.js"></script>.
- Use this shared nav markup so it matches other pages (mark the current page's link aria-current):
  a <nav class="site-nav"> with a brand link to index.html, a <button class="nav-toggle" aria-label="Menu" aria-expanded="false">, and <div class="nav-links"> containing: {nav_items}
- Reuse the existing component classes (do NOT invent a parallel system). Available classes: {class_ref or '.container, .hero, .btn, .btn-primary, .section, .card, .card-grid, .site-nav, .nav-links, .nav-toggle, .site-footer'}
- Real, specific, engaging copy tailored to the brief. Concrete details, plausible specifics.
- A real footer (.site-footer) with brand + copyright + relevant links.
- Responsive and accessible. Wrap page content sections in .container where appropriate.

MOTION — add scroll-reveal hooks so the page animates in:
- Put `data-reveal` on each major section/block you want to fade+rise into view (hero content, section
  headers, standalone blocks).
- Put `data-reveal-group` on a container whose CHILDREN should stagger in (e.g. the .card-grid, a
  pricing-grid, a stat row). Do not also put data-reveal on those same children.

IMAGES — never reference an external or local image file (there are none; they render broken):
- Do NOT emit <img src="...jpg/png/webp"> or CSS url() to a file. For any photo/illustration slot, use a
  placeholder block: <div class="media-ph"><span class="media-ph__label">short description of the intended image</span></div>
  (add data-shape="square" or "portrait" to change its ratio). Inline SVG icons/logos are fine and encouraged.

If the page includes an FAQ, use: <div class="faq"><button class="faq-q" aria-expanded="false">Question<span aria-hidden="true">+</span></button><div class="faq-a"><div class="container">Answer</div></div></div> (the toggle JS is already provided).

Return ONLY the HTML (no markdown fences, no commentary)."""
    # Cinematic homepages carry the payoff sections (services/pricing/proof/FAQ/CTA) below an
    # auto-injected intro — they're the longest pages and the ones the audit caught truncating at
    # 4096. Give them a generous ceiling; regenerate once if the model still runs out of room.
    budget = 8000 if cinematic_home else 4096
    html = _strip_code_fence(_call(claude, system, user, max_tokens=budget), "html")
    if _is_truncated(html):
        retry = _strip_code_fence(_call(claude, system, user, max_tokens=8000), "html")
        # Take the retry if it completed, or at least if it got further before running out.
        if not _is_truncated(retry) or len(retry) > len(html):
            html = retry
    return html


# ============================================================
# STAGE 4 — SELF-REVIEW POLISH (stylesheet hardening)
# ============================================================
def self_review(claude, plan: dict, css: str, sample_html: str, used_classes: set) -> tuple:
    """Additive polish pass. Instead of rewriting styles.css (which risks dropping class
    definitions the pages rely on), the reviewer returns an APPENDED refinement layer —
    overrides and additions only. The base stylesheet is always preserved, so every class
    the pages use stays defined."""
    system = (
        "You are a meticulous design reviewer. You improve an existing site by writing a small "
        "CSS 'polish layer' that is APPENDED after the base stylesheet (so later rules override "
        "earlier ones via the cascade). You NEVER rewrite the whole sheet and NEVER remove or "
        "rename classes. You refine within the established aesthetic; you do not redesign.\n\n"
        + DESIGN_SYSTEM
    )
    user = f"""Here is a site's base stylesheet and one page. Identify concrete refinements — spacing
consistency, type hierarchy, contrast, the mobile nav behavior, hover/focus states, and anything that
reads as a generic AI template — then write a COMPACT CSS polish layer that will be appended AFTER the
base to override/refine it. Only touch existing classes (listed below); do not introduce new class
names the pages don't use, do not restate rules you aren't changing.

AESTHETIC TO PRESERVE: {plan['design'].get('aesthetic')}
CLASSES THE PAGES USE (only refine these / element selectors): {', '.join('.' + c for c in sorted(used_classes))}

--- BASE styles.css ---
{css[:6000]}

--- SAMPLE PAGE ({plan['pages'][0]['filename']}) ---
{sample_html[:3000]}

Return ONLY the polish-layer CSS (no fences, no full-sheet rewrite). Then a new line starting
`REVIEW_NOTES:` with 2-4 short points (; separated) on what you refined."""
    # Generous budget: this analytical prompt makes the model think heavily before emitting CSS;
    # too small a cap gets spent entirely on reasoning and returns no stylesheet.
    raw = _call(claude, system, user, max_tokens=6000)
    notes = ""
    if "REVIEW_NOTES:" in raw:
        css_part, notes_part = raw.rsplit("REVIEW_NOTES:", 1)
        notes = notes_part.strip()
        raw = css_part
    layer = _strip_code_fence(raw, "css")
    # Guard: the layer must look like CSS and not be a sneaky full rewrite that could still
    # clobber via specificity games. If it's implausible, skip it.
    if len(layer) < 40 or "{" not in layer:
        return css, "(self-review produced no usable polish layer — kept base stylesheet)"
    combined = css.rstrip() + "\n\n/* ===== self-review polish layer (appended) ===== */\n" + layer + "\n"
    return combined, (notes or "Applied an appended polish layer refining spacing, type, and states.")


def _patch_missing_classes(claude, plan: dict, css: str, missing: set) -> str:
    """Append rules for any classes the pages use but the stylesheet never defined."""
    d = plan["design"]
    system = ("You write CSS that matches an existing design system's variables and aesthetic. "
              "You add ONLY the requested missing class rules; you don't restate existing ones.")
    user = f"""This site's stylesheet is missing rules for some classes the pages use, so those
elements render unstyled. Write CSS rules ONLY for these classes, consistent with the design tokens
and aesthetic below. Use the existing CSS variables (var(--bg), var(--primary), etc.).

Aesthetic: {d.get('aesthetic')}
Design tokens (already defined as :root vars): {json.dumps(d.get('colors', {}))} radius {d.get('radius')}
Missing classes to define: {', '.join('.' + c for c in sorted(missing))}

Return ONLY the CSS rules (no fences, no explanation)."""
    add = _strip_code_fence(_call(claude, system, user, max_tokens=1500), "css")
    if "{" in add:
        return css.rstrip() + "\n\n/* ===== added: fills for classes used by pages ===== */\n" + add + "\n"
    return css


# ============================================================
# ORCHESTRATION
# ============================================================
MAIN_JS = """// Progressive enhancement: mobile nav, scroll reveals, FAQ accordion, sticky-nav shadow.
// Flag the document ASAP so reveal CSS only hides content when JS is actually running
// (no-JS visitors and reduced-motion users always see everything).
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce) document.documentElement.classList.add('reveal-ready');
})();

document.addEventListener('DOMContentLoaded', function () {
  // --- mobile nav toggle ---
  var toggle = document.querySelector('.nav-toggle');
  var links = document.querySelector('.nav-links');
  if (toggle && links) {
    toggle.addEventListener('click', function () {
      var open = links.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    links.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') { links.classList.remove('open'); toggle.setAttribute('aria-expanded', 'false'); }
    });
  }

  // --- sticky-nav shadow on scroll ---
  var nav = document.querySelector('.site-nav');
  if (nav) {
    var onScroll = function () { nav.classList.toggle('scrolled', window.scrollY > 8); };
    onScroll(); window.addEventListener('scroll', onScroll, { passive: true });
  }

  // --- scroll-reveal (the "Framer Motion feel", vanilla) ---
  var targets = document.querySelectorAll('[data-reveal], [data-reveal-group]');
  if (!('IntersectionObserver' in window) || document.documentElement.classList.contains('reveal-ready') === false) {
    // no observer support (or reduced motion): just show everything
    targets.forEach(function (el) { el.classList.add('in-view'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add('in-view'); io.unobserve(entry.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });
    targets.forEach(function (el) { io.observe(el); });
  }

  // --- FAQ accordion ---
  document.querySelectorAll('.faq-q').forEach(function (q) {
    q.addEventListener('click', function () {
      var open = q.getAttribute('aria-expanded') === 'true';
      q.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
  });
});
"""


def _balance_braces(css: str) -> str:
    """Repair the two malformations model-generated CSS actually produces, either of which can
    silently swallow every rule that follows (including layers appended later):
      1. A truncated function like `var(--` with an unclosed '(' — the '(' eats the next '}' so
         the rule never closes. We close such parens right before the terminating ';' or '}'.
      2. A missing rule-closing '}'. We append the shortfall so damage is contained to a segment.
    Only ever ADDS closers; never removes. (Guaranteed effect layers live in a separate file — see
    effects.css — so this is a best-effort cleanup for the model's own base sheet.)"""
    # 1) close a truncated var()/function value sitting directly before ';' or '}'
    css = re.sub(r'(\b[a-zA-Z-]+\(\s*--?[A-Za-z0-9_-]*)(\s*[;}])', r'\1)\2', css)
    # 2) balance braces (ignoring those inside comments/strings)
    probe = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    probe = re.sub(r'"(?:[^"\\]|\\.)*"', '""', probe)
    probe = re.sub(r"'(?:[^'\\]|\\.)*'", "''", probe)
    diff = probe.count('{') - probe.count('}')
    if diff > 0:
        css = css.rstrip() + "\n" + ("}" * diff) + "\n"
    return css


def _link_effects(html: str) -> str:
    """Add <link rel=stylesheet href=effects.css> right after the styles.css link so the
    guaranteed motion/cinematic layer loads as its OWN sheet — immune to any parse error in the
    model-generated styles.css. Falls back to just before </head>."""
    if re.search(r'href=["\']effects\.css["\']', html, re.I):
        return html
    link = '<link rel="stylesheet" href="effects.css">'
    m = re.search(r'<link[^>]+href=["\']styles\.css["\'][^>]*>', html, re.I)
    if m:
        i = m.end()
        return html[:i] + "\n  " + link + html[i:]
    m = re.search(r'</head>', html, re.I)
    if m:
        return html[:m.start()] + "  " + link + "\n" + html[m.start():]
    return html


def _ensure_script(html: str) -> str:
    """Guarantee <script src="main.js"> loads on every page. The page prompt asks the model
    to add it, but the model is unreliable — and it notably drops it on the cinematic homepage,
    which is the ONE page that needs main.js to run the scroll-scrub engine (without it the
    cinematic intro renders as a static sticky stack and never animates). So force it in,
    deterministically, the same way _link_effects guarantees the effect stylesheet. Idempotent:
    a page that already links main.js is left unchanged. Inserted just before </body>, else </html>,
    else appended."""
    if re.search(r'<script[^>]+src=["\']main\.js["\']', html, re.I):
        return html
    tag = '<script src="main.js"></script>'
    m = re.search(r'</body>', html, re.I)
    if m:
        return html[:m.start()] + "  " + tag + "\n" + html[m.start():]
    m = re.search(r'</html>', html, re.I)
    if m:
        return html[:m.start()] + tag + "\n" + html[m.start():]
    return html + "\n" + tag


def _is_truncated(html: str) -> bool:
    """A page whose model output ran into max_tokens stops mid-document with no closing
    </html>. That's the truncation signature the audit found on cinematic homepages."""
    return not re.search(r'</html>', html or "", re.I)


def _ensure_complete_html(html: str) -> str:
    """Guarantee the page is a COMPLETE document. Cinematic homepages (and occasionally
    ordinary pages) can truncate at the model's max_tokens — the output stops mid-tag with no
    </body>/</html>, so the browser renders a cut-off page. Mirroring _balance_braces, this is a
    deterministic best-effort repair: it only trims a trailing INCOMPLETE tag and APPENDS the
    missing document-closing tags — it never rewrites content. Idempotent: a page that already
    has </html> is returned unchanged. (The upstream fix is a bigger token budget + a regenerate
    retry in build_page; this is the last-resort guarantee, the same role _balance_braces plays
    for the stylesheet.)"""
    if not _is_truncated(html):
        return html
    repaired = (html or "").rstrip()
    # If it ends mid-tag (an unmatched '<' after the last '>'), drop the partial tag so we
    # don't leave a broken `<div cla…` fragment visible.
    lt, gt = repaired.rfind("<"), repaired.rfind(">")
    if lt > gt:
        repaired = repaired[:lt].rstrip()
    if not re.search(r"</body>", repaired, re.I):
        repaired += "\n</body>"
    repaired += "\n</html>\n"
    return repaired


def _fix_images(html: str, site_dir: str) -> str:
    """Guarantee no broken images: replace every <img> whose src is a local file that
    doesn't exist (the agent generates no image assets) with a styled .media-ph placeholder
    that reuses the img's alt text. External (http/https) and inline data: images are kept."""
    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    def repl(m):
        tag = m.group(0)
        src_m = re.search(r'src\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        src = (src_m.group(1) if src_m else "").strip()
        if src.startswith(("http://", "https://", "data:")):
            return tag  # real remote / inline image — leave it
        if src and os.path.isfile(os.path.join(site_dir, src.lstrip("/"))):
            return tag  # a local file that actually exists
        alt_m = re.search(r'alt\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        label = (alt_m.group(1) if alt_m else "").strip() or "image"
        cls_m = re.search(r'class\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        extra = (" " + cls_m.group(1).strip()) if cls_m and cls_m.group(1).strip() else ""
        return (f'<div class="media-ph{_esc(extra)}" role="img" aria-label="{_esc(label)}">'
                f'<span class="media-ph__label">{_esc(label)}</span></div>')

    return re.sub(r'<img\b[^>]*?/?>', repl, html, flags=re.I)


def _serve_script(slug: str, port: int) -> str:
    return f"""#!/usr/bin/env bash
# One-command local preview for "{slug}". Serves this folder on localhost:{port}.
# (Deliberately NOT port 5001 — that's the chat app.)
cd "$(dirname "$0")" || exit 1
PORT="${{1:-{port}}}"
echo "Serving {slug} at http://localhost:$PORT  (Ctrl+C to stop)"
python3 -m http.server "$PORT"
"""


def _readme(plan: dict, port: int, review_notes: str) -> str:
    pages = "\n".join(f"- `{p['filename']}` — {p.get('purpose', p.get('nav_label',''))}"
                      for p in plan["pages"])
    d = plan["design"]
    cinema_block = ""
    scenes = plan.get("cinema_scenes")
    if scenes:
        beats = "\n".join(f"{i+1}. **{s.get('title','')}** — ideal footage: {s.get('visual','')}"
                          for i, s in enumerate(scenes))
        cinema_block = f"""

## Cinematic intro
The home page opens with a scroll-scrubbed cinematic intro ({len(scenes)} scenes) that pins full-screen
and "travels in" as you scroll, then releases into the content. Scenes:

{beats}

**These scenes use gradient placeholders — swap in real footage to make it truly cinematic.**
In `index.html`, find each `.cinema__bg` and set a real image/video still:
```html
<div class="cinema__bg" style="--scene-img: url('scene1.jpg');" ...></div>
```
Drop `scene1.jpg` (etc.) into this folder. Each scene already carries the ideal-shot description in an
HTML comment right above it. The scroll zoom, pinning, and headline timing all keep working unchanged.
"""
    return f"""# {plan.get('name')}

{plan.get('tagline')}

_Generated by the Second Brain website_creator_agent on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._

## Preview locally
```bash
bash serve.sh          # then open http://localhost:{port}
# or: bash serve.sh 9001   to use a different port
```

## Pages
{pages}

## Design system
- **Aesthetic:** {d.get('aesthetic')}
- **Fonts:** {d['fonts'].get('heading')} (headings) / {d['fonts'].get('body')} (body) — loaded via Google Fonts.
- **Palette:** {', '.join(f"{k}: {v}" for k, v in d.get('colors', {}).items())}
- All tokens live as CSS variables in `styles.css`.

## Structure
- `*.html` — the pages
- `styles.css` — the shared, hand-tuned stylesheet (single source of design truth)
- `effects.css` — guaranteed motion/cinematic layer, loaded after styles.css (kept separate so it always works)
- `main.js` — progressive-enhancement script (mobile nav, scroll-reveal animations, FAQ accordion, sticky-nav shadow)
- `serve.sh` — local preview server
{cinema_block}
## Self-review pass notes
{review_notes or '(none)'}

## Notes
- Fully static — no build step, no dependencies. Deploy by copying this folder to any static host.
- Google Fonts load from the network in-browser; the rest is self-contained.
"""


def create_website(brief: str, port: int = DEFAULT_PREVIEW_PORT, log: bool = True,
                   claude_client: Anthropic = None, supabase_client=None,
                   progress=None, cinematic: bool = False) -> dict:
    """Full pipeline. Returns {slug, dir, pages, plan, review_notes}.
    cinematic=True prepends a scroll-scrubbed 'travel-in' hero intro to the home page."""
    claude = claude_client or Anthropic(api_key=CLAUDE_API_KEY)

    def _p(msg):
        if progress:
            progress(msg)

    _p("Planning site + design tokens…")
    plan = plan_site(claude, brief)

    cinema_html = ""
    if cinematic:
        _p("Scripting the cinematic intro…")
        scenes = plan_cinema_scenes(claude, plan, brief)
        if scenes:
            cinema_html = _render_cinema(scenes, plan["design"].get("colors", {}))
            plan["cinema_scenes"] = scenes  # surfaced in README

    slug = plan["slug"]
    site_dir = os.path.join(SITES_DIR, slug)
    # no-clobber: suffix if the slug dir already exists
    n = 1
    while os.path.exists(site_dir):
        site_dir = os.path.join(SITES_DIR, f"{slug}-{n}")
        n += 1
    os.makedirs(site_dir, exist_ok=True)
    slug = os.path.basename(site_dir)

    _p("Building the stylesheet…")
    css, class_ref = build_stylesheet(claude, plan)
    css = _balance_braces(css)  # contain any unclosed rule to the base sheet

    _p(f"Writing {len(plan['pages'])} page(s)…")
    written_pages = []
    first_html = ""
    used_classes = set()
    for i, page in enumerate(plan["pages"]):
        is_cinematic_home = bool(cinema_html) and i == 0
        html = build_page(claude, plan, page, class_ref, cinematic_home=is_cinematic_home)
        html = _ensure_complete_html(html)   # never ship a truncated document (audit finding #3)
        html = _fix_images(html, site_dir)   # no broken images ever ship
        html = _link_effects(html)           # load the guaranteed effect layer as its own sheet
        html = _ensure_script(html)          # guarantee main.js loads (esp. the cinematic home)
        if is_cinematic_home:
            html = _inject_cinema(html, cinema_html)  # prepend the scroll intro after the nav
        with open(os.path.join(site_dir, page["filename"]), "w", encoding="utf-8") as f:
            f.write(html)
        written_pages.append(page["filename"])
        for m in re.findall(r'class="([^"]*)"', html):
            used_classes.update(m.split())
        if i == 0:
            first_html = html

    _p("Self-review polish pass…")
    css, review_notes = self_review(claude, plan, css, first_html, used_classes)
    css = _balance_braces(css)

    # The guaranteed motion + cinematic layers live in a SEPARATE effects.css (loaded after
    # styles.css). Isolating them means no parse error in the model's styles.css can ever swallow
    # them — the class of bug where a truncated var() or unclosed brace killed the whole effect.
    effects_css = MOTION_CSS + ("\n" + CINEMATIC_CSS if cinema_html else "")

    # Coverage guard: every class the pages use must be defined SOMEWHERE (styles.css OR
    # effects.css). Only ask the model to fill rules that neither sheet defines.
    defined = set(re.findall(r'\.([a-zA-Z][\w-]*)', css + effects_css))
    missing = {c for c in used_classes if c and c not in defined}
    if missing:
        _p(f"Filling {len(missing)} missing style rule(s)…")
        css = _patch_missing_classes(claude, plan, css, missing)
    css = _balance_braces(css)  # final safety on the model's own sheet

    # write shared assets
    with open(os.path.join(site_dir, "styles.css"), "w", encoding="utf-8") as f:
        f.write(css)
    with open(os.path.join(site_dir, "effects.css"), "w", encoding="utf-8") as f:
        f.write("/* Guaranteed effect layer — motion" + (" + cinematic intro" if cinema_html else "")
                + ". Loaded after styles.css. */\n" + effects_css)
    with open(os.path.join(site_dir, "main.js"), "w", encoding="utf-8") as f:
        f.write(MAIN_JS + (CINEMATIC_JS if cinema_html else ""))
    serve_path = os.path.join(site_dir, "serve.sh")
    with open(serve_path, "w", encoding="utf-8") as f:
        f.write(_serve_script(slug, port))
    os.chmod(serve_path, 0o755)
    with open(os.path.join(site_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(_readme(plan, port, review_notes))

    result = {"slug": slug, "dir": site_dir, "pages": written_pages,
              "plan": plan, "review_notes": review_notes, "port": port}
    if log:
        _log_to_supabase(brief, result, supabase_client)
    return result


def _log_to_supabase(brief, result, supabase_client=None):
    try:
        sb = supabase_client or create_client(SUPABASE_URL, SUPABASE_KEY)
        summary = {
            "brief": brief[:500],
            "site": result["plan"].get("name"),
            "slug": result["slug"],
            "pages": result["pages"],
            "dir": os.path.relpath(result["dir"], os.path.dirname(os.path.abspath(__file__))),
            "preview": f"bash {os.path.relpath(result['dir'])}/serve.sh  → http://localhost:{result['port']}",
            "aesthetic": result["plan"]["design"].get("aesthetic"),
        }
        sb.table("Agent Outputs").insert(
            {"agent_name": AGENT_NAME, "output_text": json.dumps(summary, indent=2)}
        ).execute()
    except Exception as e:
        print(f"Warning: couldn't log to Supabase: {e}", file=sys.stderr)


# ============================================================
# CHAT-BRAIN ENTRY POINT
# ============================================================
_CINEMATIC_HINTS = ("cinematic", "immersive", "scroll", "flashy", "eye-catching", "eye catching",
                    "movie", "dramatic", "storytelling", "travel in", "zoom in")

# ---- Idempotency guard -------------------------------------------------------
# The chat model occasionally emits create_website TWICE for a single request,
# producing two redundant builds (each costs several sequential model calls and
# a few minutes). This guard makes one request produce exactly one build:
#   * a lock serializes builds so two calls can never build concurrently;
#   * a short-TTL cache keyed by the normalized brief returns the first build's
#     result string for any identical brief seen within the window, instead of
#     rebuilding. The second (duplicate) tool call therefore returns instantly
#     with a note, and no second site directory is created.
_BUILD_LOCK = threading.Lock()
_RECENT_BUILDS = {}            # normalized_brief -> (finished_at_epoch, result_string)
_IDEMPOTENCY_TTL_SECONDS = 300


def _normalize_brief(brief: str) -> str:
    return " ".join((brief or "").lower().split())


def create_website_for_chat(brief: str, claude_client=None, supabase_client=None,
                            cinematic=None) -> str:
    # auto-enable the cinematic intro when the brief asks for that kind of energy
    if cinematic is None:
        cinematic = any(h in brief.lower() for h in _CINEMATIC_HINTS)

    key = _normalize_brief(brief)
    if not key:
        return "I need a brief describing the site to build — what's it for, and roughly what pages?"

    # Serialize builds and dedupe identical briefs within the TTL window. Holding
    # the lock across the whole build means a duplicate second call waits for the
    # first to finish, then finds the cached result rather than starting its own.
    with _BUILD_LOCK:
        now = time.time()
        # prune stale cache entries so it can't grow unbounded
        for k in [k for k, (ts, _) in _RECENT_BUILDS.items() if now - ts > _IDEMPOTENCY_TTL_SECONDS]:
            _RECENT_BUILDS.pop(k, None)
        cached = _RECENT_BUILDS.get(key)
        if cached and now - cached[0] <= _IDEMPOTENCY_TTL_SECONDS:
            return (cached[1] +
                    "\n\n_(This matched a site I just built moments ago, so I reused that build "
                    "instead of making a duplicate.)_")

        try:
            r = create_website(brief, claude_client=claude_client, supabase_client=supabase_client,
                               cinematic=cinematic)
        except Exception as e:
            return (f"Website build failed: {e}\n\n"
                    "Nothing was saved. This is usually a transient model/network hiccup — "
                    "try again, and if it keeps failing, simplify the brief a little.")
        rel = os.path.relpath(r["dir"], os.path.dirname(os.path.abspath(__file__)))
        msg = (f"Built **{r['plan'].get('name')}** — {r['plan'].get('tagline')}\n"
               f"{len(r['pages'])} pages: {', '.join(r['pages'])}\n"
               f"Aesthetic: {r['plan']['design'].get('aesthetic')}\n\n"
               f"Saved to `{rel}/`. Preview it with:\n```\nbash {rel}/serve.sh\n```\n"
               f"then open http://localhost:{r['port']}. A README with the design system is in the folder.")
        _RECENT_BUILDS[key] = (time.time(), msg)
        return msg


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Generate a static website from a brief.")
    parser.add_argument("--brief", help="The site brief (purpose, pages, style notes).")
    parser.add_argument("--brief-file", help="Path to a file containing the brief.")
    parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT,
                        help=f"Local preview port (default {DEFAULT_PREVIEW_PORT}).")
    parser.add_argument("--cinematic", action="store_true",
                        help="Prepend a scroll-scrubbed cinematic 'travel-in' intro to the home page.")
    args = parser.parse_args()

    for name, val in [("CLAUDE_API_KEY", CLAUDE_API_KEY)]:
        if not val:
            sys.exit(f"Missing required environment variable: {name}")

    brief = args.brief
    if args.brief_file:
        with open(args.brief_file, encoding="utf-8") as f:
            brief = f.read()
    if not brief:
        sys.exit("Provide --brief or --brief-file.")

    print("Building site…")
    r = create_website(brief, port=args.port, progress=lambda m: print("  •", m),
                       cinematic=args.cinematic)
    print(f"\nDone → {r['dir']}")
    print(f"Pages: {', '.join(r['pages'])}")
    print(f"Preview: bash {os.path.relpath(r['dir'])}/serve.sh  (http://localhost:{r['port']})")


if __name__ == "__main__":
    main()
