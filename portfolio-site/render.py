#!/usr/bin/env python3
"""Turn the scaffold into a deployable site once Alex has named the service.

The site is the last thing gated on the naming decision, and the point of this
script is that the decision costs one command instead of an editing pass:

    python3 portfolio-site/render.py --name "Northrun" --email hello@northrun.com

Output lands in `portfolio-site/dist/` (gitignored, safe to delete and re-render).
Nothing here touches the scaffold, so re-rendering under a different name is free
and the name is never half-applied across the two files.

It also refuses to emit a site that would break two of the plan's rules:

  1. **The word "AI" appears in no client-facing artifact.** Checked on the
     rendered output, with word boundaries — "email" and "detail" are fine,
     "AI-powered" is not.
  2. **No unapproved brand is shown as work.** The three spec packs were built
     unsolicited from public ads; Portland Pet Food, Golde and Fishwife have
     never heard of us. Naming them under a heading called "work" on a public
     page implies a client relationship that does not exist, so the default is
     category labels. Real names require --brands AND --brands-approved, which
     is a claim Alex makes deliberately, not a default he backs into.
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
SOURCES = ("index.html", "style.css")

# Honest by default: what the packs ARE (unsolicited samples in a category),
# not who they were built from.
DEFAULT_BRANDS = ("pet food brand", "wellness brand", "food & beverage brand")

# Rendered output must not contain these. "AI" is word-boundary + case-sensitive so
# a service name like "Aimwell" doesn't trip it; the phrases are case-insensitive.
BANNED = [
    (re.compile(r"\bAI\b"), "the letters AI"),
    (re.compile(r"artificial intelligence", re.I), "'artificial intelligence'"),
    (re.compile(r"\bLLM\b", re.I), "'LLM'"),
    (re.compile(r"\b(GPT|Claude|OpenAI|Anthropic|ChatGPT)\b", re.I), "a model/vendor name"),
    (re.compile(r"\bmachine learning\b", re.I), "'machine learning'"),
    (re.compile(r"\bautomat(ed|ion|ically)\b", re.I), "an automation tell"),
]


def render(name: str, email: str, brands: tuple, out_dir: str) -> list:
    mapping = {
        "{{SERVICE_NAME}}": name,
        "{{CONTACT_EMAIL}}": email,
        "{{brand_1}}": brands[0],
        "{{brand_2}}": brands[1],
        "{{brand_3}}": brands[2],
    }
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for fname in SOURCES:
        src = os.path.join(HERE, fname)
        text = open(src, encoding="utf-8").read()
        for token, value in mapping.items():
            text = text.replace(token, value)
        dest = os.path.join(out_dir, fname)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        written.append(dest)
    return written


def audit(paths: list) -> list:
    """Every reason the rendered site must not ship. Empty list means clean."""
    problems = []
    for path in paths:
        text = open(path, encoding="utf-8").read()
        base = os.path.basename(path)

        # A survivor here means a placeholder was renamed in the scaffold and this
        # script wasn't updated — exactly the silent half-branded page to prevent.
        for leftover in sorted(set(re.findall(r"\{\{[^}]+\}\}", text))):
            problems.append(f"{base}: unreplaced placeholder {leftover}")

        for pattern, label in BANNED:
            for m in pattern.finditer(text):
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{base}:{line}: contains {label} — '{m.group(0)}'")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description="Render the portfolio site.")
    p.add_argument("--name", required=True, help="the service name (blocks everything)")
    p.add_argument("--email", required=True, help="contact address on the domain")
    p.add_argument("--brands", nargs=3, metavar=("B1", "B2", "B3"),
                   help="three spec-pack labels; defaults to category labels")
    p.add_argument("--brands-approved", action="store_true",
                   help="assert those brands approved being named publicly")
    p.add_argument("--out", default=DIST, help=f"output dir (default {DIST})")
    p.add_argument("--force", action="store_true", help="overwrite an existing dist/")
    args = p.parse_args()

    name, email = args.name.strip(), args.email.strip()
    if not name:
        print("error: --name cannot be blank.", file=sys.stderr)
        return 2
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        print(f"error: --email doesn't look like an address: {email!r}", file=sys.stderr)
        return 2

    brands = tuple(args.brands) if args.brands else DEFAULT_BRANDS
    if args.brands and not args.brands_approved:
        print("error: naming real brands under 'work' claims a client relationship.\n"
              "       The three spec packs were built unsolicited from public ads — those\n"
              "       brands have not been contacted, let alone agreed to be listed.\n"
              "       Re-run with --brands-approved once they actually have, or drop\n"
              "       --brands to use honest category labels.", file=sys.stderr)
        return 2

    if os.path.isdir(args.out) and not args.force:
        if os.listdir(args.out):
            print(f"error: {args.out} exists and is not empty; pass --force to overwrite.",
                  file=sys.stderr)
            return 2
    if args.force and os.path.isdir(args.out):
        shutil.rmtree(args.out)

    written = render(name, email, brands, args.out)

    problems = audit(written)
    if problems:
        print("REFUSED — the rendered site breaks a plan rule:\n", file=sys.stderr)
        for prob in problems:
            print(f"  ✗ {prob}", file=sys.stderr)
        shutil.rmtree(args.out, ignore_errors=True)   # never leave a bad site on disk
        return 1

    print(f"Rendered '{name}' → {args.out}")
    for path in written:
        print(f"  {os.path.basename(path)}  ({os.path.getsize(path):,} bytes)")
    print(f"  spec-pack labels: {', '.join(brands)}"
          f"{'  (declared approved)' if args.brands_approved else '  (category labels)'}")
    print("\nClean: no placeholders left, no 'AI', no unapproved brand names.")
    print(f"Preview locally:  python3 -m http.server -d {args.out} 8080")
    return 0


if __name__ == "__main__":
    sys.exit(main())
