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
        "visual identity — never a generic AI template."
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
# STAGE 2 — STYLESHEET
# ============================================================
def build_stylesheet(claude, plan: dict) -> tuple:
    d = plan["design"]
    nav = " · ".join(p.get("nav_label", "") for p in plan["pages"])
    system = (
        "You are a front-end engineer with strong visual taste. You write clean, modern, "
        "hand-crafted CSS that reads as intentional design — not a bootstrap-looking template. "
        "You use CSS custom properties, fluid type (clamp), flex/grid, and tasteful detail "
        "(considered spacing, hover states, subtle motion). Mobile-first and fully responsive."
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
- Fluid, responsive, accessible contrast, visible :focus-visible states, prefers-reduced-motion respect.
- Give it real personality consistent with the aesthetic. NO generic centered-everything AI look.

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
def build_page(claude, plan: dict, page: dict, class_ref: str) -> str:
    d = plan["design"]
    nav_items = "".join(
        f'<a href="{p["filename"]}">{p.get("nav_label", "")}</a>' for p in plan["pages"]
    )
    other_pages = ", ".join(f'{p["nav_label"]} ({p["filename"]})' for p in plan["pages"])
    system = (
        "You are a web developer AND a sharp copywriter. You write complete, semantic, accessible "
        "HTML5 pages with REAL, specific, on-brand copy — never lorem ipsum, never vague filler "
        "like 'we provide quality solutions'. Every headline and paragraph is concrete and useful."
    )
    user = f"""Write the COMPLETE HTML for `{page['filename']}` of this site.

Site: {plan.get('name')} — {plan.get('tagline')}
Audience: {plan.get('audience')} · Tone: {plan.get('tone')}
This page's purpose: {page.get('purpose')}
Sections to include (in order, with real content): {', '.join(page.get('sections', []))}
Other pages (for nav + internal links): {other_pages}

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

Return ONLY the HTML (no markdown fences, no commentary)."""
    html = _strip_code_fence(_call(claude, system, user, max_tokens=4096), "html")
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
        "rename classes. You refine within the established aesthetic; you do not redesign."
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
MAIN_JS = """// Mobile nav toggle — progressive enhancement.
document.addEventListener('DOMContentLoaded', function () {
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
});
"""


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
- `main.js` — small progressive-enhancement script (mobile nav toggle)
- `serve.sh` — local preview server

## Self-review pass notes
{review_notes or '(none)'}

## Notes
- Fully static — no build step, no dependencies. Deploy by copying this folder to any static host.
- Google Fonts load from the network in-browser; the rest is self-contained.
"""


def create_website(brief: str, port: int = DEFAULT_PREVIEW_PORT, log: bool = True,
                   claude_client: Anthropic = None, supabase_client=None,
                   progress=None) -> dict:
    """Full pipeline. Returns {slug, dir, pages, plan, review_notes}."""
    claude = claude_client or Anthropic(api_key=CLAUDE_API_KEY)

    def _p(msg):
        if progress:
            progress(msg)

    _p("Planning site + design tokens…")
    plan = plan_site(claude, brief)
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

    _p(f"Writing {len(plan['pages'])} page(s)…")
    written_pages = []
    first_html = ""
    used_classes = set()
    for i, page in enumerate(plan["pages"]):
        html = build_page(claude, plan, page, class_ref)
        with open(os.path.join(site_dir, page["filename"]), "w", encoding="utf-8") as f:
            f.write(html)
        written_pages.append(page["filename"])
        for m in re.findall(r'class="([^"]*)"', html):
            used_classes.update(m.split())
        if i == 0:
            first_html = html

    _p("Self-review polish pass…")
    css, review_notes = self_review(claude, plan, css, first_html, used_classes)

    # Coverage guard: every class the pages use must be defined in the final CSS. If any are
    # missing, ask the model to add the missing rules (keeps the pages from rendering unstyled).
    defined = set(re.findall(r'\.([a-zA-Z][\w-]*)', css))
    missing = {c for c in used_classes if c and c not in defined}
    if missing:
        _p(f"Filling {len(missing)} missing style rule(s)…")
        css = _patch_missing_classes(claude, plan, css, missing)

    # write shared assets
    with open(os.path.join(site_dir, "styles.css"), "w", encoding="utf-8") as f:
        f.write(css)
    with open(os.path.join(site_dir, "main.js"), "w", encoding="utf-8") as f:
        f.write(MAIN_JS)
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
def create_website_for_chat(brief: str, claude_client=None, supabase_client=None) -> str:
    try:
        r = create_website(brief, claude_client=claude_client, supabase_client=supabase_client)
    except Exception as e:
        return f"Website build failed: {e}"
    rel = os.path.relpath(r["dir"], os.path.dirname(os.path.abspath(__file__)))
    return (f"Built **{r['plan'].get('name')}** — {r['plan'].get('tagline')}\n"
            f"{len(r['pages'])} pages: {', '.join(r['pages'])}\n"
            f"Aesthetic: {r['plan']['design'].get('aesthetic')}\n\n"
            f"Saved to `{rel}/`. Preview it with:\n```\nbash {rel}/serve.sh\n```\n"
            f"then open http://localhost:{r['port']}. A README with the design system is in the folder.")


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Generate a static website from a brief.")
    parser.add_argument("--brief", help="The site brief (purpose, pages, style notes).")
    parser.add_argument("--brief-file", help="Path to a file containing the brief.")
    parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT,
                        help=f"Local preview port (default {DEFAULT_PREVIEW_PORT}).")
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
    r = create_website(brief, port=args.port, progress=lambda m: print("  •", m))
    print(f"\nDone → {r['dir']}")
    print(f"Pages: {', '.join(r['pages'])}")
    print(f"Preview: bash {os.path.relpath(r['dir'])}/serve.sh  (http://localhost:{r['port']})")


if __name__ == "__main__":
    main()
