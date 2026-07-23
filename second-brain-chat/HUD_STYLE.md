# HUD_STYLE.md

**Mandatory design spec for all CLARVIS UI work.** Every screen, component, and
one-off visual must conform to this spec exactly. When in doubt, err toward more
fine detail and stricter adherence — never less.

CLARVIS renders as a film-style "Iron Man" heads-up display: glowing cyan
strokes on deep navy, dense with decorative telemetry, thin circuit traces, and
slowly rotating rings. It is a HUD, not a dashboard of cards.

---

## Palette

The **only** colors permitted anywhere in the UI:

The **only** colors permitted anywhere in the UI. Values are sampled directly
from the reference (`references/hud-target.png`) — a turquoise-cyan on
near-black navy. The tint/glow alphas all derive from the primary's RGB
`79, 212, 232`.

| Role                | Value       | Usage                                                        |
| ------------------- | ----------- | ----------------------------------------------------------- |
| Background (edges)  | `#010a20`   | Outer stops of the page radial gradient                     |
| Background (center) | `#123a6e`   | Center stop of the page radial gradient                     |
| Primary cyan        | `#4fd4e8`   | Default stroke, text, borders, glow color (rgb 79,212,232)  |
| Secondary blue      | `#2f6aa8`   | Supporting strokes, fills, de-emphasized structural elements|
| Bright accent       | `#9bf2fa`   | Highlights, active states, emphasis strokes/text            |
| White               | `#e8fdff`   | **Reserved for key numbers only** — never body text or UI   |

The page background is a radial gradient from `#123a6e` at the center to
`#010a20` at the edges:

```css
background: radial-gradient(ellipse at center, #123a6e 0%, #010a20 100%);
```

**No other colors anywhere.** No greens, ambers, reds, greys, or off-brand
blues — not for status, not for errors, not for charts. Convey state through
brightness, glow intensity, and opacity within this palette only. `#e8fdff`
white is reserved strictly for key numeric readouts; do not use it for labels,
paragraphs, icons, or chrome.

---

## Glow

Glow is the signature of the HUD. It lives on **strokes and text only** — never
on panels.

- Every stroke and every text element carries:
  ```css
  filter: drop-shadow(0 0 4px rgba(79, 212, 232, 0.6));
  ```
- For emphasis elements (key numbers, active gauges, primary headings), **stack
  a second drop-shadow** for a brighter, wider bloom:
  ```css
  filter: drop-shadow(0 0 4px rgba(79, 212, 232, 0.6))
          drop-shadow(0 0 12px rgba(155, 242, 250, 0.5));
  ```
- **Never use `box-shadow` on panels.** Panels have no glow of their own; the
  glow radiates from the strokes and text drawn on and around them.
- Glow color stays within the palette — cyan `rgba(79,212,232,…)` by default,
  bright accent `rgba(155,242,250,…)` for the emphasis layer.

---

## Panels

Panels are drawn frames, not cards.

- **Transparent fills.** Maximum fill is `rgba(79, 212, 232, 0.05)`; usually fully
  transparent. **No solid card backgrounds ever.**
- **1px cyan borders** (`#4fd4e8`), thin and glowing per the Glow rules.
- **Clipped 45-degree corner notches** instead of rounded corners. Use
  `clip-path` to cut the corners at 45°:
  ```css
  clip-path: polygon(
    12px 0, 100% 0, 100% calc(100% - 12px),
    calc(100% - 12px) 100%, 0 100%, 0 12px
  );
  ```
- **Corner brackets** — short L-shaped strokes at the panel corners — reinforce
  the frame. Draw them as pseudo-elements or SVG, never as border-radius.
- No `border-radius` on panels. Nothing is a rounded rectangle.

---

## Typography

- **Orbitron** (Google Fonts) — headings and all numbers.
- **Share Tech Mono** (Google Fonts) — labels and body-adjacent text.
- **All uppercase**, everywhere.
- **letter-spacing: 0.15em** on all text.
- **Labels are tiny: 10–11px.**
- Numbers may be larger and are the visual anchors; key numbers use the reserved
  white `#e8fdff` and the stacked-glow emphasis filter.

```css
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400..900&family=Share+Tech+Mono&display=swap');

.hud-heading, .hud-number { font-family: 'Orbitron', sans-serif; }
.hud-label { font-family: 'Share Tech Mono', monospace; font-size: 10px; }
.hud-heading, .hud-number, .hud-label {
  text-transform: uppercase;
  letter-spacing: 0.15em;
}
```

---

## Lines

- **Thin strokes: 1–1.5px.** Never heavier.
- **Dashed tick rings** — concentric rings marked with dashes/ticks around
  gauges and the central reactor element.
- **Circuit traces** bend only at **90° or 45°** angles and **terminate in small
  dots or squares**. No curves in traces, no arbitrary angles.
- Strokes glow per the Glow rules.

---

## Motion

Motion is slow, continuous, and mechanical. Nothing bounces; nothing eases
dramatically.

- **Rotation:** rings rotate continuously, **20–60s per revolution, `linear`
  timing**. Different rings at different speeds/directions.
- **Pulse:** gauges carry a subtle opacity pulse (small amplitude, slow).
- **Scanline:** an occasional scanline sweep passes across the interface.
- Forbidden: bounce, elastic, dramatic ease-in/out, springy or playful motion.

```css
@keyframes hud-rotate { to { transform: rotate(360deg); } }
.hud-ring { animation: hud-rotate 40s linear infinite; }

@keyframes hud-pulse { 0%,100% { opacity: 0.85; } 50% { opacity: 1; } }
.hud-gauge { animation: hud-pulse 4s ease-in-out infinite; }
```

---

## Density

Match the decorative density of a film-style HUD. **Sparse layouts look wrong —
err toward more fine detail.** Fill negative space with functional-looking
telemetry:

- Tick marks and graduated scales
- Small hex clusters
- Dotted / dashed grids
- Binary digit streams as filler (`0101001100011001`)
- Micro decimal readouts (`5.232145`, `8.235687`)
- Chevron runs (`>>>>>`), corner brackets, small dot/square terminators

Every region should read as instrumented. When a layout feels empty, add fine
decorative detail rather than enlarging existing elements.
