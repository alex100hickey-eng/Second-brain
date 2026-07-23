# HUD Frame Techniques — borrowed from Arwes

Reference source: `references/arwes` (arwes/arwes, `packages/frames`) — kept locally,
**not imported**. Arwes is React + the `motion` lib; the techniques below are
re-expressed for CLARVIS's stack (vanilla SVG + GSAP, served from `/static/vendor/`)
and our palette. Follow [HUD_STYLE.md](HUD_STYLE.md) for colors, glow, and motion feel.

Key source files skimmed:
- `packages/frames/src/createFrameCornersSettings/createFrameCornersSettings.ts` — corner brackets
- `packages/frames/src/animateFrameAssembler/animateFrameAssembler.ts` — animated frame assembly
- `packages/animated/src/.../animateDraw.ts` — stroke "draw-on" animation
- `packages/frames/src/styleFrameClipOctagon/styleFrameClipOctagon.ts` — 45° notch clip-path
- `packages/frames/src/createFrameNefrexSettings/createFrameNefrexSettings.ts` — asymmetric notched HUD frame

---

## 1. Corner brackets (not borders)

Arwes draws a frame as **8 short stroked path segments** — two L-forming lines per
corner — over a transparent/near-transparent `rect`. Corners use percentage-relative
coordinates so the frame is fully responsive; `cornerLength` (arwes default 16px) sets
bracket size, `strokeWidth/2` is the stroke offset `co` that keeps strokes inside the box.

Each corner is two paths, e.g. left-top:
- `M co,co  L co,cornerLength`   (vertical arm)
- `M co,co  L cornerLength,co`   (horizontal arm)

Right/bottom corners mirror using `100% - co` for x/y. Group style: `fill:none`,
`stroke-linecap:round`, `stroke-linejoin:round`, thin `stroke-width`. This is exactly
our "corner brackets instead of rounded rectangles" rule.

Vanilla SVG we can drop into a panel (swap `--len`/`--sw` as needed):
```html
<svg class="hud-frame" preserveAspectRatio="none" viewBox="0 0 100 100">
  <!-- transparent bg (max 5% cyan per HUD_STYLE) -->
  <rect x="0.5" y="0.5" width="99" height="99" fill="rgba(79,212,232,0.03)" stroke="none"/>
  <g fill="none" stroke="#4fd4e8" stroke-width="1"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M0.5 16 L0.5 0.5 L16 0.5"/>            <!-- left-top   -->
    <path d="M84 0.5 L99.5 0.5 L99.5 16"/>          <!-- right-top  -->
    <path d="M99.5 84 L99.5 99.5 L84 99.5"/>        <!-- right-bot  -->
    <path d="M16 99.5 L0.5 99.5 L0.5 84"/>          <!-- left-bot   -->
  </g>
</svg>
```
Note `preserveAspectRatio="none"` + a `viewBox` lets one SVG stretch to any panel size.
Apply the cyan glow via `filter: drop-shadow(...)` on the `<g>` (strokes only), per spec.

## 2. Draw-on animation (the signature "assembling" look)

`animateDraw` = the classic stroke-dashoffset trick:
1. `len = path.getTotalLength()`
2. `strokeDasharray = len; strokeDashoffset = len` → path invisible
3. animate `strokeDashoffset` from `len → 0` → line draws itself in
4. reverse (`0 → len`) to un-draw on exit; clear the inline props when done

GSAP version (we have gsap at `/static/vendor/gsap.min.js`):
```js
function drawOn(pathEl, duration = 0.8) {
  const len = pathEl.getTotalLength();
  gsap.set(pathEl, { strokeDasharray: len, strokeDashoffset: len });
  return gsap.to(pathEl, { strokeDashoffset: 0, duration, ease: "sine.out" });
}
```
Keep easing calm (`sine.out` / `expo.out`) — matches HUD_STYLE's "nothing bounces".

## 3. Frame assembler (layered reveal)

`animateFrameAssembler` sequences a frame's parts by `data-name`:
- `[data-name=bg]`  — fade opacity `0 → 1` over the first half
- `[data-name=deco]`— flicker in `opacity: [0, 1, 0.5, 1]` in the second half
- `[data-name=line]`— all draw-on simultaneously (dashoffset `len → 0`)

Exit just reverses the timeline. Tag SVG children with `data-name` and build one GSAP
timeline:
```js
function assembleFrame(root, duration = 1.0) {
  const tl = gsap.timeline();
  tl.fromTo(root.querySelectorAll('[data-name=bg]'),   {opacity:0}, {opacity:1, duration:duration/2, ease:"sine.out"}, 0);
  tl.fromTo(root.querySelectorAll('[data-name=deco]'), {opacity:0}, {opacity:1, duration:duration/2, ease:"sine.out"}, duration/2);
  root.querySelectorAll('[data-name=line]').forEach(p => tl.add(drawOn(p, duration), 0));
  return tl;
}
```
Good for CLARVIS panels mounting in / route changes — reads as the HUD "booting up".

## 4. 45° notched corners (clip-path)

`styleFrameClipOctagon` generates a `clip-path: polygon(...)` that cuts any/all corners
at 45°, `squareSize` controlling the notch (default 16px). Each corner is independently
toggleable. This is the pure-CSS way to get our "clipped 45-degree corner notches":
```css
.hud-panel {
  clip-path: polygon(
    16px 0, calc(100% - 16px) 0, 100% 16px,
    100% calc(100% - 16px), calc(100% - 16px) 100%,
    16px 100%, 0 calc(100% - 16px), 0 16px
  ); /* all four corners notched; drop a pair of points to keep a square corner */
}
```
For a **glowing** notched edge you can't stroke a clip-path — draw the outline as an SVG
`<path>` matching the same polygon and glow that (clip shapes the fill, SVG path shows the lit edge).

## 5. Asymmetric HUD frame (Nefrex) — for hero panels

`createFrameNefrexSettings` is the iconic angular frame: by default only `leftTop` +
`rightBottom` corners get a diagonal cut plus a short arm (`smallLineLength`) and a long
arm (`largeLineLength` ~64px) running along the edge. Path uses relative commands
(`v/h/l`) so it scales. Use sparingly for a primary/hero panel; keep the plain corner
brackets (§1) for the many small telemetry panels to hold density without visual noise.

---

### Takeaways for CLARVIS
- Frames are **stroked SVG paths**, never CSS `border` — that's what lets us draw them on
  and glow them per stroke.
- One responsive corner-bracket SVG (§1) is the default panel treatment; §4 notch clips
  the fill to match.
- GSAP timelines (§2/§3) give the "assembling" reveal without pulling in React/motion.
- Everything stays within the palette and glow rules in HUD_STYLE.md.
