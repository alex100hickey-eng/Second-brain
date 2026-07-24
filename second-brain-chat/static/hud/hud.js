/* ============================================================
   CLARVIS HUD component library — static/hud/hud.js
   Reusable SVG/CSS/JS components per HUD_STYLE.md.
   Deps: d3 (arcs/gauges), gsap (rotation/animation).
   Geometry calibrated against references/hud-target.png.

   Every factory returns a DOM node you can drop anywhere.
   Global: window.HUD
   ============================================================ */
(function (global) {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const d3 = global.d3;
  const gsap = global.gsap;

  /* ---------- tiny DOM helpers ---------- */
  function el(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function txt(node, s) { node.textContent = s; return node; }
  function svg(w, h, extra) {
    const s = el('svg', Object.assign({
      class: 'hud-svg', viewBox: `0 0 ${w} ${h}`,
      width: w, height: h, preserveAspectRatio: 'xMidYMid meet'
    }, extra || {}));
    return s;
  }
  // polar point (0deg = up, clockwise), matches hud.html's P()
  function P(cx, cy, r, aDeg) {
    const a = aDeg * Math.PI / 180;
    return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  }
  function spin(node, cx, cy, dur, reverse) {
    if (!gsap) return;
    gsap.to(node, {
      rotation: reverse ? -360 : 360,
      svgOrigin: `${cx} ${cy}`,
      duration: dur, ease: 'none', repeat: -1
    });
  }

  const HUD = {};

  /* ============================================================
     HudPanel — clipped corners + glowing corner brackets
     opts: {width,height, title, notch=14, bracket=18, fill=true}
     Returns a <div.hud-panel>; append content into .hud-content
     or pass opts.content (a Node).
     ============================================================ */
  HUD.panel = function (opts) {
    opts = opts || {};
    const w = opts.width || 260, h = opts.height || 180;
    const notch = opts.notch == null ? 14 : opts.notch;
    const bl = opts.bracket == null ? 18 : opts.bracket; // bracket arm length
    const o = 1; // stroke inset

    const div = document.createElement('div');
    div.className = 'hud-panel';
    div.style.width = w + 'px';
    div.style.height = h + 'px';
    div.style.setProperty('--notch', notch + 'px');
    if (opts.fill === false) div.style.setProperty('--hud-fill', 'transparent');

    // frame overlay (real px coords → brackets stay square at any size)
    const frame = svg(w, h, { class: 'hud-frame' });
    // notched octagon outline (dim, glowing)
    const oc = `M ${notch} ${o} H ${w - notch} L ${w - o} ${notch} V ${h - notch}
                L ${w - notch} ${h - o} H ${notch} L ${o} ${h - notch} V ${notch} Z`;
    el('path', { d: oc.replace(/\s+/g, ' '), class: 'hud-stroke-dim', 'stroke-width': 1 }, frame);
    // four corner brackets (bright), each an L just inside the notch
    const g = el('g', { class: 'hud-stroke-bright', 'stroke-width': 1.5,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, frame);
    const b = [
      `M ${notch} ${o + bl} L ${notch} ${o} L ${notch + bl} ${o}`,          // TL
      `M ${w - notch - bl} ${o} L ${w - notch} ${o} L ${w - o} ${notch + bl}`, // TR
      `M ${w - o} ${h - notch - bl} L ${w - notch} ${h - o} L ${w - notch - bl} ${h - o}`, // BR
      `M ${notch + bl} ${h - o} L ${notch} ${h - o} L ${o} ${h - notch - bl}`  // BL
    ];
    b.forEach(d => el('path', { d }, g));
    div.appendChild(frame);

    if (opts.title) {
      const t = document.createElement('div');
      t.className = 'hud-panel-title';
      t.textContent = opts.title;
      div.appendChild(t);
    }
    const content = document.createElement('div');
    content.className = 'hud-content';
    content.style.width = '100%';
    content.style.height = '100%';
    content.style.display = 'flex';
    content.style.alignItems = 'center';
    content.style.justifyContent = 'center';
    if (opts.content) content.appendChild(opts.content);
    div.appendChild(content);
    div._content = content;
    return div;
  };

  /* ============================================================
     CoreReactor — the dashboard centerpiece.
     Concentric rotating rings + blades + glowing core.
     opts: {size=440}
     ============================================================ */
  HUD.coreReactor = function (opts) {
    opts = opts || {};
    const S = opts.size || 440;
    const c = S / 2;                 // center
    const k = S / 572;               // scale factor vs. reference (r≈286)
    const R = (v) => v * k;          // scaled radius
    const s = svg(S, S, { class: 'hud-svg', style: 'overflow:visible' });

    // ambient core glow
    const defs = el('defs', {}, s);
    const rg = el('radialGradient', { id: 'hud-core-' + Math.round(c * 1000 % 99999), cx: '50%', cy: '50%', r: '50%' }, defs);
    const gid = rg.id;
    el('stop', { offset: '0%', 'stop-color': '#ffffff' }, rg);
    el('stop', { offset: '24%', 'stop-color': '#cff8ff', 'stop-opacity': '0.96' }, rg);
    el('stop', { offset: '60%', 'stop-color': '#4fd4e8', 'stop-opacity': '0.5' }, rg);
    el('stop', { offset: '100%', 'stop-color': '#4fd4e8', 'stop-opacity': '0' }, rg);
    el('circle', { cx: c, cy: c, r: R(330), fill: `url(#${gid})`, opacity: 0.12 }, s);
    el('circle', { cx: c, cy: c, r: R(286), fill: `url(#${gid})`, opacity: 0.22 }, s);

    // geometry helpers for the ring stack (fractions of the outer radius)
    const K = S / 2, F = (f) => f * K;
    const arc = (parent, r, a0, len, sw, stroke, op, cap) => {
      const [x0, y0] = P(c, c, r, a0), [x1, y1] = P(c, c, r, a0 + len);
      return el('path', { d: `M ${x0} ${y0} A ${r} ${r} 0 ${len > 180 ? 1 : 0} 1 ${x1} ${y1}`,
        fill: 'none', stroke, 'stroke-width': sw, opacity: op == null ? 1 : op,
        'stroke-linecap': cap || 'round' }, parent);
    };
    // seeded PRNG so the glyph rings render identically every load
    let sd = 7;
    const rnd = () => (sd = (sd * 9301 + 49297) % 233280) / 233280;

    // faint straight lines crossing the whole assembly (ref has 2-3)
    const gLines = el('g', { stroke: 'var(--hud-white)', 'stroke-width': 1, opacity: 0.16 }, s);
    [[28, 1.3], [117, 1.22], [78, 1.12]].forEach(([ang, ext]) => {
      const [x0, y0] = P(c, c, F(ext), ang), [x1, y1] = P(c, c, F(ext), ang + 180);
      el('line', { x1: x0, y1: y0, x2: x1, y2: y1 }, gLines);
    });

    // sparse graduated ring behind the petals (the protractor arc that peeks
    // out under the banner)
    const gGrad = el('g', {}, s);
    el('circle', { cx: c, cy: c, r: F(0.86), class: 'hud-stroke-faint', 'stroke-width': 1 }, gGrad);
    for (let i = 0; i < 60; i++) {
      const [x1, y1] = P(c, c, F(0.83), i * 6), [x2, y2] = P(c, c, F(0.86), i * 6);
      el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)', 'stroke-width': 1, opacity: 0.35 }, gGrad);
    }

    // SIX hollow petal boxes, large, sporadically spaced and sized (ref)
    const gP = el('g', { class: 'hud-glow' }, s);
    const petals = [[15, 0.20, 1.03, 0.62], [88, 0.13, 0.96, 0.68],
                    [148, 0.23, 1.05, 0.58], [212, 0.12, 0.94, 0.66],
                    [262, 0.18, 1.01, 0.60], [325, 0.14, 0.97, 0.67]];
    petals.forEach(([ang, hw, ro, ri]) => {
      const w0 = F(hw), w1 = F(hw * 0.78), r0 = F(ro), r1 = F(ri);
      el('path', { d: `M ${-w0} ${-r0} L ${w0} ${-r0} L ${w1} ${-r1} L ${-w1} ${-r1} Z`,
        fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': 1.2, opacity: 0.8,
        transform: `rotate(${ang} ${c} ${c}) translate(${c} ${c})` }, gP);
    });
    spin(gP, c, c, 170, true);

    // chunky bright arc ring — the signature outer ring
    const gA = el('g', { class: 'hud-glow-strong' }, s);
    [[-15, 70, 1], [70, 40, 0.85], [128, 55, 0.95], [200, 80, 0.9], [300, 45, 0.8]]
      .forEach(([a0, len, op]) => arc(gA, F(0.72), a0, len, F(0.048), 'var(--hud-accent)', op));
    [[95, 18], [262, 22]].forEach(([a0, len]) =>
      arc(gA, F(0.66), a0, len, F(0.028), 'var(--hud-cyan)', 0.7));
    spin(gA, c, c, 90);

    // thin ring + stitched micro-dash ring
    el('circle', { cx: c, cy: c, r: F(0.695), class: 'hud-stroke-dim', 'stroke-width': 1,
      'stroke-dasharray': '8 6' }, s);
    el('circle', { cx: c, cy: c, r: F(0.64), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': 1, opacity: 0.4 }, s);
    el('circle', { cx: c, cy: c, r: F(0.61), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': F(0.012), opacity: 0.55, 'stroke-dasharray': '2 5' }, s);
    // extra patterned layers between the signature rings (density pass)
    el('circle', { cx: c, cy: c, r: F(0.58), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': 1, opacity: 0.3, 'stroke-dasharray': '1 3' }, s);
    const gFragA = el('g', {}, s);
    [[35, 30], [160, 22], [285, 34]].forEach(([a0, len]) =>
      arc(gFragA, F(0.565), a0, len, F(0.014), 'var(--hud-cyan)', 0.7));
    spin(gFragA, c, c, 110, true);
    el('circle', { cx: c, cy: c, r: F(0.50), class: 'hud-stroke', 'stroke-width': 1.2,
      opacity: 0.6, 'stroke-dasharray': '20 8 4 8' }, s);
    const gFragB = el('g', {}, s);
    [[75, 14], [200, 20], [318, 12]].forEach(([a0, len]) =>
      arc(gFragB, F(0.455), a0, len, F(0.016), 'var(--hud-accent)', 0.8));
    spin(gFragB, c, c, 75);
    el('circle', { cx: c, cy: c, r: F(0.415), class: 'hud-stroke-dim', 'stroke-width': F(0.010),
      'stroke-dasharray': '1 6' }, s);

    // medium arc ring
    const gM = el('g', { class: 'hud-glow' }, s);
    el('circle', { cx: c, cy: c, r: F(0.53), class: 'hud-stroke-faint', 'stroke-width': 1 }, gM);
    [[20, 55, 0.9], [95, 35, 0.7], [150, 60, 0.85], [250, 50, 0.75], [330, 25, 0.6]]
      .forEach(([a0, len, op]) => arc(gM, F(0.53), a0, len, F(0.026), 'var(--hud-cyan)', op));
    spin(gM, c, c, 64, true);

    // inner structure: stitch, faint ring, short-arc cluster
    el('circle', { cx: c, cy: c, r: F(0.48), class: 'hud-stroke-dim', 'stroke-width': 1,
      'stroke-dasharray': '1 4' }, s);
    el('circle', { cx: c, cy: c, r: F(0.43), class: 'hud-stroke-faint', 'stroke-width': 1 }, s);
    const gSh = el('g', {}, s);
    [[10, 25], [70, 15], [130, 30], [200, 20], [275, 25], [335, 12]]
      .forEach(([a0, len]) => arc(gSh, F(0.38), a0, len, F(0.018), 'var(--hud-cyan)', 0.8));
    spin(gSh, c, c, 40);

    // WHITE glyph rings — irregular data-mark rings around the core
    const glyphRing = (r, sw, marks) => {
      const g = el('g', {}, s);
      let a = rnd() * 360;
      for (let i = 0; i < marks; i++) {
        arc(g, r, a, 4 + rnd() * 14, sw, 'var(--hud-white)', 0.92, 'butt');
        a += 8 + rnd() * 14;
      }
      return g;
    };
    el('circle', { cx: c, cy: c, r: F(0.34), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': 1, opacity: 0.3 }, s);
    el('circle', { cx: c, cy: c, r: F(0.31), class: 'hud-stroke', 'stroke-width': 1,
      opacity: 0.55, 'stroke-dasharray': '12 5' }, s);
    spin(glyphRing(F(0.26), F(0.030), 24), c, c, 52, true);
    el('circle', { cx: c, cy: c, r: F(0.225), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': 1, opacity: 0.35, 'stroke-dasharray': '2 4' }, s);
    spin(glyphRing(F(0.18), F(0.024), 18), c, c, 36);
    el('circle', { cx: c, cy: c, r: F(0.155), class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    el('circle', { cx: c, cy: c, r: F(0.13), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': F(0.008), opacity: 0.7, 'stroke-dasharray': '1.5 4' }, s);

    // glowing white core disc + halo ring
    const gCore = el('g', { class: 'hud-glow-core' }, s);
    el('circle', { cx: c, cy: c, r: F(0.24), fill: `url(#${gid})` }, gCore);
    el('circle', { cx: c, cy: c, r: F(0.115), stroke: 'var(--hud-white)', fill: 'none',
      'stroke-width': 1.5, opacity: 0.8 }, gCore);
    el('circle', { cx: c, cy: c, r: F(0.095), fill: '#ffffff' }, gCore);
    if (gsap) gsap.to(gCore, { scale: 1.05, opacity: 0.94, svgOrigin: `${c} ${c}`,
      duration: 1.7, ease: 'sine.inOut', repeat: -1, yoyo: true });

    // optional: turn the core into a link (opts.href). Topmost transparent
    // hit area over the center + hover ring affordance + tooltip.
    if (opts.href) {
      const a = el('a', {}, s);
      a.setAttribute('href', opts.href);
      a.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', opts.href);
      if (opts.linkTarget) a.setAttribute('target', opts.linkTarget);
      a.style.cursor = 'pointer';
      const hov = el('circle', { cx: c, cy: c, r: F(0.235), fill: 'none',
        stroke: 'var(--hud-accent)', 'stroke-width': 2, opacity: 0,
        'pointer-events': 'none' }, a);
      hov.style.transition = 'opacity 0.25s';
      el('circle', { cx: c, cy: c, r: F(0.28), fill: 'rgba(0,0,0,0)',
        'pointer-events': 'all' }, a);
      const tt = el('title', {}, a);
      tt.textContent = opts.linkTitle || 'Open chat';
      a.addEventListener('mouseenter', () => { hov.style.opacity = 0.9; });
      a.addEventListener('mouseleave', () => { hov.style.opacity = 0; });
    }

    return s;
  };

  /* ============================================================
     RadialGauge — dashed tick ring, slow rotation, % in center.
     opts: {size=180, value=0..100, label}
     Uses d3.arc for the value arc.
     ============================================================ */
  HUD.radialGauge = function (opts) {
    opts = opts || {};
    const S = opts.size || 180, c = S / 2, val = opts.value == null ? 72 : opts.value;
    const rOuter = S * 0.42, rTick = S * 0.46, rTickIn = S * 0.40;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;

    // rotating dashed tick ring
    const gTicks = el('g', {}, s);
    el('circle', { cx: c, cy: c, r: rTick, class: 'hud-stroke-faint', 'stroke-width': 1, 'stroke-dasharray': '2 6' }, gTicks);
    for (let i = 0; i < 60; i++) {
      const major = i % 5 === 0;
      const [x1, y1] = P(c, c, major ? rTickIn - 4 : rTickIn, i / 60 * 360);
      const [x2, y2] = P(c, c, rTick, i / 60 * 360);
      el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)', 'stroke-width': major ? 1.5 : 1, opacity: major ? 0.8 : 0.35 }, gTicks);
    }
    spin(gTicks, c, c, 48);

    // track + value arc (d3)
    const track = S * 0.09;
    const arc = d3.arc().innerRadius(rOuter - track).outerRadius(rOuter).cornerRadius(0);
    el('path', { d: arc({ startAngle: 0, endAngle: 2 * Math.PI }), transform: `translate(${c} ${c})`,
      class: 'hud-stroke-faint', fill: 'var(--hud-cyan-12)', 'stroke-width': 0 }, s);
    const end = 2 * Math.PI * (val / 100);
    el('path', { d: arc({ startAngle: 0, endAngle: end }), transform: `translate(${c} ${c})`,
      fill: 'var(--hud-cyan)', class: 'hud-glow-strong' }, s);

    // center readout
    const num = el('text', { x: c, y: c + S * 0.055, 'text-anchor': 'middle',
      'font-size': S * 0.20, class: 'hud-txt' }, s);
    txt(num, Math.round(val) + '%');
    if (opts.label) {
      const lb = el('text', { x: c, y: c + S * 0.20, 'text-anchor': 'middle',
        'font-size': S * 0.075, class: 'hud-txt-dim' }, s);
      txt(lb, opts.label);
    }
    // subtle pulse
    if (gsap) gsap.to(s, { opacity: 0.86, duration: 3.2, ease: 'sine.inOut', repeat: -1, yoyo: true });
    return s;
  };

  /* ============================================================
     DataBar — segmented horizontal progress bar.
     opts: {width=220, segments=16, value=0..100, label}
     ============================================================ */
  HUD.dataBar = function (opts) {
    opts = opts || {};
    const W = opts.width || 220, segs = opts.segments || 16, val = opts.value == null ? 62 : opts.value;
    const H = 34, gap = 3, pad = 4;
    const sw = (W - pad * 2 - gap * (segs - 1)) / segs;
    const on = Math.round(segs * val / 100);
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    el('rect', { x: 0.5, y: 8.5, width: W - 1, height: H - 12, class: 'hud-stroke-dim', 'stroke-width': 1, fill: 'none' }, s);
    for (let i = 0; i < segs; i++) {
      const filled = i < on;
      // flat: uniform cyan fill, no accent tip cell (ref: 5.232145 slot bar)
      el('rect', { x: pad + i * (sw + gap), y: 12, width: sw, height: H - 19,
        fill: filled ? (!opts.flat && i === on - 1 ? 'var(--hud-accent)' : 'var(--hud-cyan-70)') : 'var(--hud-cyan-12)',
        class: filled ? 'hud-glow' : '' }, s);
    }
    if (opts.label) {
      const lb = el('text', { x: 0, y: 6, 'font-size': 9, class: 'hud-txt-dim' }, s);
      txt(lb, opts.label);
    }
    return s;
  };

  /* ============================================================
     WaveMonitor — animated EKG-style waveform.
     opts: {width=260, height=90, label}
     ============================================================ */
  HUD.waveMonitor = function (opts) {
    opts = opts || {};
    const W = opts.width || 260, H = opts.height || 90;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const midY = H * 0.55, A = opts.amp || 1;
    let d = `M 6 ${midY}`;
    if (opts.profile) {
      // bespoke one-shot trace (ref vitals wave is a specific composition)
      for (const [dx, dy] of opts.profile) d += ` l ${dx} ${dy}`;
      d += ` H ${W - 14}`;
    } else {
      // repeating EKG; one full repeat is 22 + 58px — guard so the last
      // beat can't overshoot
      const beat = [[16, 0], [8, -6 * A], [8, 12 * A], [6, -22 * A], [8, 26 * A],
                    [6, -14 * A], [6, 4 * A]];
      let x = 6;
      while (x < W - 86) {
        d += ` h 22`; x += 22;
        for (const [dx, dy] of beat) { d += ` l ${dx} ${dy}`; x += dx; }
      }
      d += ` H ${W - 6}`;
    }
    el('path', { d, class: 'hud-stroke', 'stroke-width': opts.profile ? 2.4 : 2,
      opacity: opts.profile ? 0.85 : 0.55, 'stroke-linejoin': 'round' }, s);
    if (opts.arrow) el('polygon', {
      points: `${W - 2},${midY} ${W - 12},${midY - 5} ${W - 12},${midY + 5}`,
      fill: 'var(--hud-cyan)', class: 'hud-glow' }, s);
    const sweep = el('path', { d, class: 'hud-stroke-bright', 'stroke-width': 2, 'stroke-linejoin': 'round',
      'stroke-dasharray': '70 1400' }, s);
    if (gsap) gsap.fromTo(sweep, { strokeDashoffset: 1470 }, { strokeDashoffset: 0, duration: 3.4, ease: 'none', repeat: -1 });
    if (opts.label) txt(el('text', { x: 6, y: 14, 'font-size': 9, class: 'hud-txt-dim' }, s), opts.label);
    return s;
  };

  /* ============================================================
     HoloCone — faceted translucent pyramid on an elliptical base
     (the ref's left-mid 3D cone ornament).
     opts: {width=160, height=132, role}
     ============================================================ */
  HUD.holoCone = function (opts) {
    opts = opts || {};
    const W = opts.width || 160, H = opts.height || 132;
    const s = svg(W, H, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    // tall spike lit from the LEFT (ref): pale faces left, deep royal right;
    // apex leans left; wide base disc spills out left of the cone
    const ax = W * 0.44, ay = H * 0.02;
    const g = el('g', { class: 'hud-glow' }, s);
    el('ellipse', { cx: W * 0.40, cy: H * 0.87, rx: W * 0.42, ry: H * 0.115,
      fill: 'rgba(47,106,168,0.55)', stroke: 'var(--hud-cyan-45)', 'stroke-width': 1.2 }, g);
    const L = [W * 0.26, H * 0.87], ML = [W * 0.38, H * 0.95],
          M = [W * 0.52, H * 0.955], MR = [W * 0.66, H * 0.93], R = [W * 0.76, H * 0.82];
    const face = (a, b, fill) => el('polygon', {
      points: `${ax},${ay} ${a[0]},${a[1]} ${b[0]},${b[1]}`, fill,
      stroke: 'rgba(232,253,255,0.45)', 'stroke-width': 1,
      'stroke-linejoin': 'round' }, g);
    face(L, ML, 'rgba(155,242,250,0.55)');
    face(ML, M, 'rgba(232,253,255,0.62)');    // palest face, front-left
    face(M, MR, 'rgba(79,212,232,0.48)');
    face(MR, R, 'rgba(47,106,168,0.88)');     // dark royal right face
    // bright front fin peeling down-left out of the silhouette (ref detail)
    el('polygon', { points: `${W * 0.40},${H * 0.55} ${W * 0.28},${H * 0.98} ${W * 0.46},${H * 0.96}`,
      fill: 'rgba(155,242,250,0.5)', stroke: 'rgba(232,253,255,0.4)', 'stroke-width': 1 }, g);
    return s;
  };

  /* ============================================================
     SpeedoCluster — open bracket frame, dashed C-arcs + a hatch arc
     hugging a digital % box (the ref's left-mid "100%" element).
     opts: {value=100, role}
     ============================================================ */
  HUD.speedoCluster = function (opts) {
    opts = opts || {};
    const W = 150, H = 116, cx = 75, cy = 58;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const seg = (r, a0, a1, sw, dash, op) => {
      const [x0, y0] = P(cx, cy, r, a0), [x1, y1] = P(cx, cy, r, a1);
      el('path', { d: `M ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`,
        fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': sw,
        opacity: op || 1, 'stroke-dasharray': dash || 'none' }, s);
    };
    // tight surround hugging the box (ref): dotted arc top-right, small
    // dashes top-left, long dashes at the bottom, thin fragments beside the
    // box sides, FOUR chunky hatch blocks per side
    seg(50, -40, 60, 2, '1.5 7', 0.9);
    seg(50, 292, 322, 2, '4 5', 0.7);
    seg(50, 120, 165, 2, '10 6', 0.8);
    seg(50, 196, 241, 2, '10 6', 0.8);
    seg(43, 245, 285, 1.5, 'none', 0.55);
    seg(43, 75, 115, 1.5, 'none', 0.55);
    const hatch = (aFrom) => {
      for (let i = 0; i < 4; i++) {
        const a = aFrom + i * 14;
        const [x1, y1] = P(cx, cy, 40, a), [x2, y2] = P(cx, cy, 52, a);
        el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan-70)',
          'stroke-width': 9, 'stroke-linecap': 'butt' }, s);
      }
    };
    hatch(242); hatch(66);
    // deep-navy digital box, bright cyan border, white value
    el('rect', { x: 35, y: 39, width: 80, height: 38, fill: 'rgba(18,58,110,0.85)',
      stroke: 'var(--hud-accent)', 'stroke-width': 1.5, class: 'hud-glow' }, s);
    const t = el('text', { x: 75, y: 65, 'text-anchor': 'middle', 'font-size': 19,
      class: 'hud-txt' }, s);
    txt(t, (opts.value == null ? 100 : opts.value) + '%');
    return s;
  };

  /* ============================================================
     StatPanel — thin rect with OUTER corner brackets: three thick
     gapped donuts, chunky line stack, big % value, large hex-nut with
     a through-crosshair and pale core, decimal label (the ref's twin
     75%/100% panels). opts: {width=200, height=144, value='100',
     decimal='0.002157', role}
     ============================================================ */
  HUD.statPanel = function (opts) {
    opts = opts || {};
    const W = opts.width || 200, H = opts.height || 144;
    const s = svg(W, H, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    el('rect', { x: 6, y: 6, width: W - 12, height: H - 12, fill: 'var(--hud-fill)',
      class: 'hud-stroke', 'stroke-width': 1.2 }, s);
    // outer corner brackets, slightly proud of the rect, long arms
    const bl = 26;
    const gB = el('g', { class: 'hud-stroke-bright', 'stroke-width': 1.6, fill: 'none' }, s);
    [`M ${bl} 0 H 0 V ${bl}`, `M ${W - bl} 0 H ${W} V ${bl}`,
     `M ${W - bl} ${H} H ${W} V ${H - bl}`, `M ${bl} ${H} H 0 V ${H - bl}`]
      .forEach(d => el('path', { d }, gB));
    // three progress donuts: bright arc + dim royal remainder (ref)
    [[38, 40, 265], [76, 150, 240], [114, 250, 285]].forEach(([cy2, a0, len]) => {
      const arcTo = (from, sweep, stroke, cls) => {
        const [x0, y0] = P(30, cy2, 13, from), [x1, y1] = P(30, cy2, 13, from + sweep);
        el('path', { d: `M ${x0} ${y0} A 13 13 0 ${sweep > 180 ? 1 : 0} 1 ${x1} ${y1}`,
          fill: 'none', stroke, 'stroke-width': 8, class: cls || '' }, s);
      };
      arcTo(a0 + len, 360 - len, 'var(--hud-blue)');
      arcTo(a0, len, 'var(--hud-accent)', 'hud-glow');
    });
    // chunky rounded line stack: three full-length, one short royal
    [[118, 'var(--hud-white)', 0.9, 4], [118, 'var(--hud-white)', 0.65, 4],
     [118, 'var(--hud-cyan)', 0.9, 4], [56, 'var(--hud-blue)', 1, 5]].forEach(([w, st, op, hh], i) =>
      el('rect', { x: 56, y: 22 + i * 9, width: Math.min(w, W - 70), height: hh,
        rx: 2, fill: st, opacity: op }, s));
    // big % value
    const t = el('text', { x: 56, y: 104, 'font-size': 26, class: 'hud-txt' }, s);
    txt(t, (opts.value == null ? '100' : opts.value) + '%');
    // large hex-nut, through-crosshair, pale core
    const hx = W - 54, hy = H - 54, hr = 32;
    let hd = '';
    for (let i = 0; i < 6; i++) {
      const [x, y] = P(hx, hy, hr, 30 + i * 60);
      hd += (i ? ' L ' : 'M ') + x + ' ' + y;
    }
    el('path', { d: hd + ' Z', class: 'hud-stroke', 'stroke-width': 1.3, fill: 'none' }, s);
    el('path', { d: `M ${hx - hr - 24} ${hy} H ${hx + hr + 18}
                     M ${hx} ${hy - hr - 26} V ${hy + hr + 22}`,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    // bright stubs where the crosshair exits the hexagon
    el('path', { d: `M ${hx} ${hy - hr - 26} v 9 M ${hx} ${hy + hr + 22} v -9
                     M ${hx - hr - 24} ${hy} h 9 M ${hx + hr + 18} ${hy} h -9`,
      class: 'hud-stroke-bright', 'stroke-width': 2 }, s);
    el('circle', { cx: hx, cy: hy, r: 13, fill: 'var(--hud-white)', opacity: 0.95,
      class: 'hud-glow' }, s);
    const d2 = el('text', { x: 56, y: H - 14, 'font-size': 9, class: 'hud-txt-dim' }, s);
    txt(d2, opts.decimal || '0.002157');
    return s;
  };

  /* ============================================================
     EcgPanel — the ref's vitals frame: chamfer/notch outline with a
     royal offset shadow, dark fill, tab notches, deco squares, bright
     chevrons, wave-start node, broken baseline, decimal label.
     Content (the wave) is overlaid by the template.
     opts: {width=285, height=140}
     ============================================================ */
  HUD.ecgPanel = function (opts) {
    opts = opts || {};
    const W = opts.width || 285, H = opts.height || 140;
    const s = svg(W, H, { style: 'overflow:visible' });
    // outline with a bite notch on the top edge and a step on the bottom
    const d = 'M 20 6 H 96 l 5 5 h 22 l 5 -5 H 238 L 278 27 V 106 L 254 134 ' +
              'H 150 l 0 -4 h -28 l 0 4 H 34 L 14 118 V 26 Z';
    el('path', { d, fill: 'none', stroke: 'var(--hud-blue)', 'stroke-width': 1.5,
      opacity: 0.85, transform: 'translate(-5,-6)' }, s);
    el('path', { d, fill: 'rgba(6,20,48,0.62)', class: 'hud-stroke',
      'stroke-width': 1.8 }, s);
    // doubled inner line along the top-right chamfer
    el('line', { x1: 236, y1: 12, x2: 271, y2: 30, class: 'hud-stroke-dim',
      'stroke-width': 1 }, s);
    // faint inner inset outline segments (bottom + right)
    el('path', { d: 'M 40 126 H 246 M 270 40 V 100', class: 'hud-stroke-faint',
      'stroke-width': 1 }, s);
    // tab notches riding the frame edges + a bright block on the top edge
    [[58, 3, 16, 5], [206, 3, 12, 5], [10, 60, 5, 18], [10, 92, 5, 12],
     [168, 131, 14, 5]].forEach(([x, y, w, h]) =>
      el('rect', { x, y, width: w, height: h, fill: 'var(--hud-cyan)', opacity: 0.7 }, s));
    el('rect', { x: 36, y: 2, width: 12, height: 8, fill: 'var(--hud-accent)',
      class: 'hud-glow' }, s);
    // dark deco squares scattered top-left + mid-left
    [[30, 20, 11], [47, 24, 7], [62, 19, 6], [26, 78, 8]].forEach(([x, y, q]) =>
      el('rect', { x, y, width: q, height: q, fill: 'rgba(8,22,50,0.95)',
        class: 'hud-stroke-faint', 'stroke-width': 1 }, s));
    // bright chevrons: top-right + bottom-left
    const chev = (x0, y0, n, sc) => {
      for (let i = 0; i < n; i++)
        el('path', { d: `M ${x0 + i * 9 * sc} ${y0} l ${7 * sc} ${5 * sc} l ${-7 * sc} ${5 * sc}`,
          fill: 'none', class: 'hud-stroke-bright', 'stroke-width': 2.2 }, s);
    };
    chev(226, 14, 3, 1);
    chev(30, 102, 3, 1.15);
    // node square where the wave begins + broken baseline + decimal
    el('rect', { x: 56, y: 62, width: 6, height: 6, fill: 'var(--hud-cyan)' }, s);
    el('line', { x1: 34, y1: 124, x2: 246, y2: 124, class: 'hud-stroke-dim',
      'stroke-width': 1, 'stroke-dasharray': '34 9' }, s);
    txt(el('text', { x: 148, y: 112, 'font-size': 9, class: 'hud-txt-dim' }, s),
      '0.0015741');
    return s;
  };

  /* ============================================================
     NodeSquare — solid square outline, four filled corner nodes,
     outer L-brackets (the ref's left-column ornament).
     opts: {size=46, node 'dot'|'square', role}
     ============================================================ */
  HUD.nodeSquare = function (opts) {
    opts = opts || {};
    const S = opts.size || 46, inset = S * 0.16, q = S * 0.14;
    const s = svg(S, S, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    el('rect', { x: inset, y: inset, width: S - inset * 2, height: S - inset * 2,
      class: 'hud-stroke', 'stroke-width': 1.4, fill: 'var(--hud-fill)' }, s);
    const pos = [[inset + q, inset + q], [S - inset - q, inset + q],
                 [inset + q, S - inset - q], [S - inset - q, S - inset - q]];
    pos.forEach(([x, y]) => opts.node === 'square'
      ? el('rect', { x: x - q / 2, y: y - q / 2, width: q, height: q,
          fill: 'var(--hud-accent)', class: 'hud-glow' }, s)
      : el('circle', { cx: x, cy: y, r: q * 0.78, fill: 'var(--hud-accent)',
          class: 'hud-glow' }, s));
    const bl = S * 0.24;
    const gB = el('g', { class: 'hud-stroke-dim', 'stroke-width': 1.2, fill: 'none' }, s);
    [[0, 0, 1, 1], [S, 0, -1, 1], [S, S, -1, -1], [0, S, 1, -1]].forEach(([x, y, dx, dy]) =>
      el('path', { d: `M ${x + dx * bl} ${y} H ${x} V ${y + dy * bl}` }, gB));
    return s;
  };

  /* ============================================================
     BarStack — stack of rounded bars, mixed tones/lengths (the ref's
     left-edge ornament). opts: {bars: [[len, tone 0|1|2]...], dir 'h'|'v',
     thick=7, gap=6, role}
     ============================================================ */
  HUD.barStack = function (opts) {
    opts = opts || {};
    const bars = opts.bars || [[44, 1], [30, 0], [38, 1], [22, 0], [34, 2]];
    const t = opts.thick || 7, gap = opts.gap || 6, v = opts.dir === 'v';
    const long = Math.max.apply(null, bars.map(b => b[0]));
    const across = bars.length * (t + gap) - gap;
    const s = svg(v ? across : long, v ? long : across);
    if (opts.role) s.dataset.role = opts.role;
    const tone = ['var(--hud-blue)', 'var(--hud-cyan)', 'var(--hud-accent)'];
    bars.forEach(([len, tn], i) => {
      const off = i * (t + gap);
      if (opts.seg && v) {
        // mostly-solid royal bars with sparse breaks; one bright tip (ref)
        let y = long;
        let k = 0;
        while (y > long - len) {
          if ((k + i * 2) % 5 === 4) { y -= 6; k++; continue; }
          const sh = Math.min(14, y - (long - len));
          el('rect', { x: off, y: y - sh, width: t, height: sh,
            fill: (i === 2 && k === 0) ? 'var(--hud-accent)' : tone[tn] }, s);
          y -= sh + 2; k++;
        }
        return;
      }
      const base = v ? { x: off, y: long - len, width: t, height: len }
                     : { x: 0, y: off, width: len, height: t };
      el('rect', Object.assign(base, { rx: t / 2, fill: tone[tn],
        class: tn === 2 ? 'hud-glow' : '' }), s);
    });
    return s;
  };

  /* ============================================================
     CircuitTrace — connector lines with 45/90 bends ending in dots.
     opts: {width=220, height=120, paths:[d,...], node=true}
     Provides a decorative default if no paths given.
     ============================================================ */
  HUD.circuitTrace = function (opts) {
    opts = opts || {};
    const W = opts.width || 220, H = opts.height || 120;
    const s = svg(W, H);
    const paths = opts.paths || [
      `M 8 20 H ${W * 0.4} l 22 22 H ${W - 40} l 20 -20 H ${W - 8}`,
      `M 8 ${H - 24} h ${W * 0.3} l 24 -24 V 46 l 20 -20 h ${W * 0.35}`,
      `M ${W * 0.5} ${H - 8} V ${H * 0.55} l -18 -18 H 40`
    ];
    const g = el('g', { class: 'hud-stroke', 'stroke-width': opts.strokeWidth || 1.5,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, s);
    // terminators: start defaults to a small accent square, end to an open dot;
    // either can be 'none' (plain line ends, e.g. gauge-to-gauge links)
    const startStyle = opts.start || 'square', endStyle = opts.end || 'dot';
    paths.forEach(d => {
      const p = el('path', { d }, g);
      if (opts.node !== false) {
        const len = p.getTotalLength ? p.getTotalLength() : 0;
        if (len) {
          const pt = p.getPointAtLength(len);
          const start = p.getPointAtLength(0);
          if (endStyle === 'dot')
            el('circle', { cx: pt.x, cy: pt.y, r: 3.2, fill: '#06203f', stroke: 'var(--hud-cyan)', 'stroke-width': 1.2 }, g);
          else if (endStyle === 'fdot')
            el('circle', { cx: pt.x, cy: pt.y, r: 3, fill: 'var(--hud-cyan)', 'stroke-width': 0 }, g);
          if (startStyle === 'square')
            el('rect', { x: start.x - 2.4, y: start.y - 2.4, width: 4.8, height: 4.8, fill: 'var(--hud-accent)' }, g);
          else if (startStyle === 'circle')
            el('circle', { cx: start.x, cy: start.y, r: 3.2, fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': 1.2 }, g);
          else if (startStyle === 'fdot')
            el('circle', { cx: start.x, cy: start.y, r: 3, fill: 'var(--hud-cyan)', 'stroke-width': 0 }, g);
        }
      }
    });
    return s;
  };

  /* ============================================================
     StatReadout — small label + large glowing number.
     opts: {label, value, unit}
     ============================================================ */
  HUD.statReadout = function (opts) {
    opts = opts || {};
    const wrap = document.createElement('div');
    if (opts.role) wrap.dataset.role = opts.role;
    wrap.style.display = 'inline-flex';
    wrap.style.flexDirection = 'column';
    wrap.style.alignItems = 'flex-start';
    wrap.style.gap = '3px';
    wrap.style.padding = '4px 8px';
    const labelText = opts.label == null ? 'READOUT' : opts.label;
    const row = document.createElement('div');
    row.style.display = 'flex';
    row.style.alignItems = 'baseline';
    row.style.gap = '4px';
    const num = document.createElement('span');
    num.className = 'hud-num';
    num.style.fontSize = (opts.size || 30) + 'px';
    num.style.lineHeight = '1';
    num.textContent = opts.value == null ? '000' : opts.value;
    row.appendChild(num);
    if (opts.unit) {
      const u = document.createElement('span');
      u.className = 'hud-heading';
      u.style.fontSize = '12px';
      u.textContent = opts.unit;
      row.appendChild(u);
    }
    if (labelText !== '') {
      const lb = document.createElement('div');
      lb.className = 'hud-label';
      lb.textContent = labelText;
      wrap.appendChild(lb);
    }
    wrap.appendChild(row);
    return wrap;
  };

  /* ============================================================
     PieIndicator — small pie-chart style indicator (d3.arc).
     opts: {size=90, value=0..100}
     ============================================================ */
  HUD.pieIndicator = function (opts) {
    opts = opts || {};
    const S = opts.size || 90, c = S / 2, r = S * 0.42, val = opts.value == null ? 35 : opts.value;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    const arc = d3.arc().innerRadius(0).outerRadius(r);
    const g = el('g', { transform: `translate(${c} ${c})`, class: 'hud-glow' }, s);
    if (opts.slices) {
      // exploded multi-slice pie (ref): gaps between slices, mixed tones
      const styles = { bright: ['rgba(155,242,250,0.9)', 'var(--hud-accent)'],
                       mid: ['rgba(79,212,232,0.55)', 'var(--hud-cyan)'],
                       dim: ['rgba(47,106,168,0.8)', 'var(--hud-blue)'] };
      let a = opts.rotate == null ? -0.9 : opts.rotate;
      opts.slices.forEach(([pct, st]) => {
        const end2 = a + 2 * Math.PI * pct / 100;
        const [f, sk] = styles[st] || styles.mid;
        el('path', { d: arc({ startAngle: a, endAngle: end2 }), fill: f,
          stroke: sk, 'stroke-width': 1.2 }, g);
        a = end2 + 0.10;
      });
      el('circle', { cx: c, cy: c, r: r + 6, class: 'hud-stroke-faint',
        'stroke-width': 1, 'stroke-dasharray': '3 4' }, s);
      return s;
    }
    const end = 2 * Math.PI * (val / 100);
    // remainder (faint)
    el('path', { d: arc({ startAngle: end, endAngle: 2 * Math.PI }), fill: 'var(--hud-cyan-12)',
      stroke: 'var(--hud-cyan-45)', 'stroke-width': 1 }, g);
    // value slice (bright)
    el('path', { d: arc({ startAngle: 0, endAngle: end }), fill: 'rgba(155,242,250,0.55)',
      stroke: 'var(--hud-accent)', 'stroke-width': 1.5 }, g);
    el('circle', { cx: c, cy: c, r: r + 5, class: 'hud-stroke-faint', 'stroke-width': 1, 'stroke-dasharray': '3 4' }, s);
    return s;
  };

  /* ============================================================
     BatteryV — vertical battery: cased rounded outline, terminal
     nub, stacked cells filling bottom-up (ref right side).
     opts: {width=64, height=110, cells=5, value=75, role}
     ============================================================ */
  HUD.batteryV = function (opts) {
    opts = opts || {};
    const W = opts.width || 64, H = opts.height || 110, cells = opts.cells || 5;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    el('rect', { x: W * 0.28, y: 2, width: W * 0.44, height: 7, rx: 2,
      class: 'hud-stroke', 'stroke-width': 1.5, fill: 'var(--hud-cyan-25)' }, s);
    el('rect', { x: 6, y: 9, width: W - 12, height: H - 12, rx: 8,
      class: 'hud-stroke', 'stroke-width': 1.6, fill: 'var(--hud-fill)' }, s);
    const ch = (H - 26) / cells;
    const lit = Math.round(cells * (opts.value == null ? 75 : opts.value) / 100);
    for (let i = 0; i < cells; i++) {
      const filled = i >= cells - lit;
      el('rect', { x: 13, y: 14 + i * ch, width: W - 26, height: ch - 5, rx: 2,
        fill: filled ? (i >= cells - 2 ? 'var(--hud-accent)' : 'var(--hud-cyan)')
                     : 'var(--hud-cyan-12)', opacity: filled ? 0.9 : 1 }, s);
    }
    return s;
  };

  /* ============================================================
     BinaryStream — animated stream of 1s and 0s (filler).
     opts: {rows=6, cols=22, cell=13}
     ============================================================ */
  HUD.binaryStream = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 6, cols = opts.cols || 22, cell = opts.cell || 13;
    const W = cols * cell, H = rows * (cell + 2);
    const s = svg(W, H);
    const cells = [];
    for (let r = 0; r < rows; r++) for (let cc = 0; cc < cols; cc++) {
      const t = el('text', { x: cc * cell, y: r * (cell + 2) + cell,
        'font-size': cell - 2, 'font-family': "'Share Tech Mono', monospace",
        fill: (r + cc) % 5 === 0 ? 'var(--hud-accent)' : 'var(--hud-cyan-45)' }, s);
      t.textContent = (r * 7 + cc * 3) % 2;
      cells.push(t);
    }
    // shimmer + occasional bit flips via gsap ticker-ish timeline
    if (gsap) {
      let i = 0;
      const flip = () => {
        for (let n = 0; n < 6; n++) {
          const t = cells[(i * 13 + n * 97) % cells.length];
          t.textContent = t.textContent === '0' ? '1' : '0';
        }
        i++;
      };
      gsap.to({}, { duration: 0.28, repeat: -1, onRepeat: flip });
      cells.forEach((t, n) => gsap.to(t, { opacity: 0.35, duration: 1.4 + (n % 5) * 0.2,
        ease: 'sine.inOut', repeat: -1, yoyo: true, delay: (n % 7) * 0.15 }));
    }
    return s;
  };

  /* ============================================================
     ScanlineOverlay — full-screen scanline/grid + sweep.
     opts: {sweep=true}. Appends to <body> (or opts.parent).
     ============================================================ */
  HUD.scanlineOverlay = function (opts) {
    opts = opts || {};
    const div = document.createElement('div');
    div.className = 'hud-scanlines';
    (opts.parent || document.body).appendChild(div);
    if (opts.sweep !== false && gsap) {
      const bar = document.createElement('div');
      bar.style.cssText = 'position:absolute;left:0;right:0;height:180px;pointer-events:none;' +
        'background:linear-gradient(180deg,transparent,rgba(155,242,250,0.05) 50%,transparent);';
      div.appendChild(bar);
      gsap.fromTo(bar, { top: '-20%' }, { top: '110%', duration: 7, ease: 'none', repeat: -1, repeatDelay: 4 });
    }
    return div;
  };

  /* ============================================================
     Dial — needle/compass gauge (data-ready).
     opts: {size=160, value=0..100, angle?, label}
     value maps to a -135..+135 speedometer sweep unless angle given.
     ============================================================ */
  HUD.dial = function (opts) {
    opts = opts || {};
    const S = opts.size || 160, c = S / 2, val = opts.value == null ? 68 : opts.value;
    const rOut = S * 0.46;
    const s = svg(S, S);
    s.dataset.role = opts.role || 'dial';
    el('circle', { cx: c, cy: c, r: rOut, class: 'hud-stroke', 'stroke-width': 1.5 }, s);
    el('circle', { cx: c, cy: c, r: rOut - 6, class: 'hud-stroke-faint', 'stroke-width': 1, 'stroke-dasharray': '2 5' }, s);
    for (let i = 0; i < 48; i++) {
      const major = i % 12 === 0;
      const [x1, y1] = P(c, c, major ? rOut - 11 : rOut - 6, i / 48 * 360);
      const [x2, y2] = P(c, c, rOut, i / 48 * 360);
      el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)', 'stroke-width': 1, opacity: major ? 0.9 : 0.4 }, s);
    }
    el('path', { d: `M ${c - rOut} ${c} H ${c + rOut} M ${c} ${c - rOut} V ${c + rOut}`,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    const ang = opts.angle != null ? opts.angle : (-135 + (val / 100) * 270);
    const grp = el('g', {}, s);
    const [nx, ny] = P(c, c, rOut * 0.84, ang);
    el('path', { d: `M ${c} ${c} L ${nx} ${ny}`, class: 'hud-stroke-bright', 'stroke-width': 2.5, 'stroke-linecap': 'round' }, grp);
    el('circle', { cx: nx, cy: ny, r: 3.6, fill: 'var(--hud-accent)', class: 'hud-glow' }, grp);
    if (gsap) gsap.to(grp, { rotation: '+=3', svgOrigin: `${c} ${c}`, duration: 2.6, ease: 'sine.inOut', repeat: -1, yoyo: true });
    el('circle', { cx: c, cy: c, r: S * 0.07, class: 'hud-stroke', 'stroke-width': 1.5, fill: 'var(--hud-cyan-12)' }, s);
    if (opts.label) txt(el('text', { x: c, y: c + rOut + 14, 'text-anchor': 'middle', 'font-size': S * 0.075, class: 'hud-txt-dim' }, s), opts.label);
    return s;
  };

  /* ============================================================
     Equalizer — animated spectrum bars (data-ready).
     opts: {bars=20, barWidth=5, gap=5, height=48}
     ============================================================ */
  HUD.equalizer = function (opts) {
    opts = opts || {};
    const bars = opts.bars || 20, bw = opts.barWidth || 5, gap = opts.gap || 5, H = opts.height || 48;
    // baseline: {left: px} extends a base rule left of bar 0 to a circle node (ref: top-left EQ)
    const ext = opts.baseline ? (opts.baseline.left == null ? 24 : opts.baseline.left) : 0;
    const W = ext + bars * (bw + gap);
    const s = svg(W, H + (opts.baseline ? 12 : 0));
    s.dataset.role = opts.role || 'equalizer';
    const seed = [8, 16, 11, 26, 19, 38, 26, 46, 33, 22, 40, 17, 30, 12, 21, 9, 15, 24, 10, 18, 28, 14, 34, 20];
    // mixed: dim steel-blue bars interleaved with bright, per the reference skyline
    const tone = (i) => opts.mixed
      ? (i % 5 === 2 ? 'var(--hud-blue)' : i % 4 === 1 ? 'var(--hud-accent)' : 'var(--hud-cyan-70)')
      : (i % 4 === 1 ? 'var(--hud-accent)' : 'var(--hud-cyan-70)');
    for (let i = 0; i < bars; i++) {
      // decay: heights slope off left-to-right with jitter (top-center mini chart)
      const base = opts.decay
        ? Math.max(4, (1 - i / bars) * 40 + (seed[i % seed.length] - 20) * 0.25)
        : seed[i % seed.length];
      const h0 = 6 + (base / 46) * (H - 6);
      const r = el('rect', { x: ext + i * (bw + gap), y: H - h0, width: bw, height: h0,
        fill: tone(i), class: opts.mixed && i % 5 === 2 ? '' : 'hud-glow' }, s);
      if (gsap) {
        // decay bars wobble around their own height so the slope survives
        const h1 = opts.decay ? Math.max(4, h0 * 0.72)
          : 6 + Math.max(5, (H - 10) * (((i * 13) % 9) / 9));
        gsap.to(r, { attr: { height: h1, y: H - h1 }, duration: 0.5 + (i % 5) * 0.12,
          ease: 'sine.inOut', repeat: -1, yoyo: true, delay: (i % 7) * 0.08 });
      }
    }
    if (opts.baseline) {
      // the ref baseline floats a clear gap below the bars, bars never touch it
      el('line', { x1: opts.baseline.cap === false ? 2 : 8, y1: H + 9, x2: W - 2, y2: H + 9,
        class: 'hud-stroke', 'stroke-width': 1 }, s);
      if (opts.baseline.cap !== false)
        el('circle', { cx: 4.5, cy: H + 9, r: 3.2, fill: 'none',
          class: 'hud-stroke', 'stroke-width': 1.2 }, s);
    }
    return s;
  };

  /* ============================================================
     DonutGauge — thick solid annulus with a bright value arc and
     a large centered readout. The reference's 30/50/70 gauges:
     dark track, cyan fill from 12 o'clock, no tick ring.
     opts: {size=76, value, thickness, role}
     ============================================================ */
  HUD.donutGauge = function (opts) {
    opts = opts || {};
    const S = opts.size || 76, c = S / 2, val = opts.value == null ? 50 : opts.value;
    // ticks: true adds a slowly rotating dashed tick ring around the donut
    // (radialGauge's halo); the donut shrinks to leave room for it.
    const rO = opts.ticks ? S * 0.38 : S * 0.48;
    const th = opts.thickness || rO * 0.4;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    if (opts.ticks) {
      const rTick = S * 0.46, rTickIn = S * 0.40;
      const gTicks = el('g', {}, s);
      el('circle', { cx: c, cy: c, r: rTick, class: 'hud-stroke-faint', 'stroke-width': 1, 'stroke-dasharray': '2 6' }, gTicks);
      for (let i = 0; i < 60; i++) {
        const major = i % 5 === 0;
        const [x1, y1] = P(c, c, major ? rTickIn - 4 : rTickIn, i / 60 * 360);
        const [x2, y2] = P(c, c, rTick, i / 60 * 360);
        el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)', 'stroke-width': major ? 1.5 : 1, opacity: major ? 0.8 : 0.35 }, gTicks);
      }
      spin(gTicks, c, c, 48);
    }
    const arc = d3.arc().innerRadius(rO - th).outerRadius(rO);
    // dark track ring
    el('path', { d: arc({ startAngle: 0, endAngle: 2 * Math.PI }), transform: `translate(${c} ${c})`,
      fill: 'rgba(79, 212, 232, 0.14)', stroke: 'var(--hud-cyan-25)', 'stroke-width': 1 }, s);
    // value arc from 12 o'clock, clockwise
    el('path', { d: arc({ startAngle: 0, endAngle: 2 * Math.PI * (val / 100) }),
      transform: `translate(${c} ${c})`, fill: 'var(--hud-cyan)', class: 'hud-glow' }, s);
    const num = el('text', { x: c, y: c + S * 0.075, 'text-anchor': 'middle',
      'font-size': S * 0.21, class: 'hud-txt' }, s);
    txt(num, Math.round(val) + '%');
    return s;
  };

  /* ============================================================
     Candlesticks — small market-style chart ornament (ref: top-left,
     right of the dashed square). Wicks + bodies, mixed bright/dim.
     opts: {count=5, width=96, height=66, role}
     ============================================================ */
  HUD.candlesticks = function (opts) {
    opts = opts || {};
    const n = opts.count || 5, W = opts.width || 96, H = opts.height || 66;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const step = W / n;
    // [wickTop, bodyTop, bodyH, wickBot, bright] as fractions of H
    const spec = [[0.10, 0.30, 0.34, 0.86, false], [0.02, 0.16, 0.44, 0.92, true],
                  [0.14, 0.34, 0.30, 0.78, false], [0.06, 0.22, 0.48, 0.96, true],
                  [0.18, 0.38, 0.28, 0.82, false]];
    for (let i = 0; i < n; i++) {
      const [wt, bt, bh, wb, bright] = spec[i % spec.length];
      const cx = step * (i + 0.5), bw = step * 0.52;
      el('line', { x1: cx, y1: H * wt, x2: cx, y2: H * wb,
        class: 'hud-stroke', 'stroke-width': 1 }, s);
      el('rect', { x: cx - bw / 2, y: H * bt, width: bw, height: H * bh,
        fill: bright ? 'var(--hud-cyan)' : 'var(--hud-blue)',
        class: bright ? 'hud-glow' : '' }, s);
    }
    return s;
  };

  /* ============================================================
     DashedSquare — dashed-outline box ornament.
     cornerStyle: 'brackets' (L strokes outside the dashes),
                  'squares'  (solid squares sitting on the corners),
                  'none'     (plain broken-line box, e.g. the EQ frame).
     opts: {size|width+height, bracket, inset, dash, cornerStyle, role}
     ============================================================ */
  HUD.dashedSquare = function (opts) {
    opts = opts || {};
    const W = opts.width || opts.size || 64, H = opts.height || opts.size || 64;
    const corner = opts.cornerStyle || 'brackets';
    const inset = opts.inset != null ? opts.inset : Math.min(W, H) * 0.14;
    const s = svg(W, H, { style: 'overflow: visible' });
    if (opts.role) s.dataset.role = opts.role;
    el('rect', { x: inset, y: inset, width: W - inset * 2, height: H - inset * 2,
      rx: opts.rx || 0, class: 'hud-stroke', 'stroke-width': 1.2,
      'stroke-dasharray': opts.dash || '5 4' }, s);
    if (corner === 'brackets') {
      const bl = opts.bracket || Math.min(W, H) * 0.22, o = 1;
      const g = el('g', { class: 'hud-stroke-bright', 'stroke-width': 1.5,
        'stroke-linecap': 'round' }, s);
      [`M ${o} ${bl} V ${o} H ${bl}`, `M ${W - bl} ${o} H ${W - o} V ${bl}`,
       `M ${W - o} ${H - bl} V ${H - o} H ${W - bl}`, `M ${bl} ${H - o} H ${o} V ${H - bl}`]
        .forEach(d => el('path', { d }, g));
    } else if (corner === 'squares') {
      // broken-line square OUTSIDE, four solid squares tucked inside its
      // corners with a clear gap (actual-ref style)
      const q = Math.min(W, H) * 0.20, p = inset + Math.min(W, H) * 0.10;
      [[p, p], [W - p - q, p], [p, H - p - q], [W - p - q, H - p - q]]
        .forEach(([x, y]) => el('rect', { x, y, width: q, height: q,
          fill: 'var(--hud-accent)', class: 'hud-glow' }, s));
    }
    return s;
  };

  /* ============================================================
     DiagCandles — 45-degree circuit rails carrying rounded capsule
     bars, dot-terminated (actual-ref ornament right of the square).
     opts: {size=110, role}
     ============================================================ */
  HUD.diagCandles = function (opts) {
    opts = opts || {};
    const S = opts.size || 110;
    const s = svg(S, S, { style: 'overflow: visible' });
    if (opts.role) s.dataset.role = opts.role;
    // rails as [x1,y1,x2,y2, capsule-ts...] climbing to the upper right
    const rails = [
      [6, S - 10, S - 30, 26, [0.30, 0.62]],
      [30, S - 2, S - 4, 40, [0.22, 0.50, 0.76]]
    ];
    rails.forEach(([x1, y1, x2, y2, caps]) => {
      el('line', { x1, y1, x2, y2, class: 'hud-stroke', 'stroke-width': 1 }, s);
      [[x1, y1], [x2, y2]].forEach(([cx, cy]) =>
        el('circle', { cx, cy, r: 3, fill: 'var(--hud-cyan)', class: 'hud-glow' }, s));
      const ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
      caps.forEach((t, i) => {
        const cx = x1 + (x2 - x1) * t, cy = y1 + (y2 - y1) * t;
        const len = 24, w = 8;
        el('rect', { x: cx - len / 2, y: cy - w / 2, width: len, height: w,
          rx: w / 2, transform: `rotate(${ang} ${cx} ${cy})`,
          fill: i % 2 ? 'var(--hud-blue)' : 'var(--hud-cyan)',
          class: i % 2 ? '' : 'hud-glow' }, s);
      });
    });
    // one plain thin companion trace, dot-tipped
    el('line', { x1: S * 0.02, y1: S * 0.72, x2: S * 0.52, y2: S * 0.22,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    el('circle', { cx: S * 0.52, cy: S * 0.22, r: 2.4, fill: 'var(--hud-cyan)' }, s);
    return s;
  };

  /* ============================================================
     HatchStripes — pack of parallel 45-degree stripes, alternating
     royal-blue thick / cyan thin (actual-ref accent block).
     opts: {count=7, gap=8, len=56, role}
     ============================================================ */
  HUD.hatchStripes = function (opts) {
    opts = opts || {};
    const n = opts.count || 7, gap = opts.gap || 8, L = opts.len || 56;
    const W = n * gap + L, H = L;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    for (let i = 0; i < n; i++) {
      const thick = i % 2 === 0;
      // trim alternate stripes for the ragged pack edge of the ref
      const trim = (i % 3) * L * 0.12;
      el('line', { x1: i * gap + trim, y1: H - trim, x2: i * gap + L - trim, y2: trim,
        stroke: thick ? 'var(--hud-blue)' : 'var(--hud-cyan)',
        'stroke-width': thick ? 4 : 2,
        class: thick ? '' : 'hud-glow' }, s);
    }
    return s;
  };

  /* ============================================================
     FloorGrid — perspective floor strip for the bottom edge: the
     ONLY grid in the HUD (background elsewhere stays clean navy).
     opts: {width=1600, height=170, vanishX}
     ============================================================ */
  /* ============================================================
     FloorText — big lettering lying flat on the floor plane, tilted
     about its top edge so it recedes toward the floor's vanishing
     point. Anchor it at the horizon; it lays out toward the viewer.
     opts: {text, size=130, tilt=68, perspective=700, spacing=0.14,
            opacity=0.9, role}
     ============================================================ */
  HUD.floorText = function (opts) {
    opts = opts || {};
    const d = document.createElement('div');
    d.className = 'hud-floor-text';
    d.textContent = opts.text || 'C.L.A.R.V.I.S';
    if (opts.role) d.dataset.role = opts.role;
    d.style.cssText =
      `font-size:${opts.size || 130}px;` +
      `letter-spacing:${opts.spacing == null ? 0.14 : opts.spacing}em;` +
      `opacity:${opts.opacity == null ? 0.9 : opts.opacity};` +
      `transform:translateX(-50%) perspective(${opts.perspective || 700}px) ` +
      `rotateX(${opts.tilt == null ? 68 : opts.tilt}deg);`;
    return d;
  };

  /* ============================================================
     OrbitRing — a ring of marks that slowly orbits a center point.
     Drop it behind the reactor to fill the space around it.
     kind: 'dots' | 'ticks' | 'arcs'
     opts: {r=310, count=28, kind, dur=90, reverse, weight, role}
     ============================================================ */
  HUD.orbitRing = function (opts) {
    opts = opts || {};
    const r = opts.r || 310, S = (r + 18) * 2, c = S / 2;
    const s = svg(S, S, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    const g = el('g', {}, s);
    const n = opts.count || 28, kind = opts.kind || 'dots';
    if (kind === 'arcs') {
      (opts.segs || [[10, 34], [96, 18], [150, 44], [232, 26], [300, 30]])
        .forEach(([a0, len]) => {
          const [x0, y0] = P(c, c, r, a0), [x1, y1] = P(c, c, r, a0 + len);
          el('path', { d: `M ${x0} ${y0} A ${r} ${r} 0 0 1 ${x1} ${y1}`, fill: 'none',
            stroke: 'var(--hud-accent)', 'stroke-width': opts.weight || 3,
            'stroke-linecap': 'round', opacity: 0.7, class: 'hud-glow' }, g);
        });
    } else {
      for (let i = 0; i < n; i++) {
        const a = i / n * 360, major = i % 4 === 0;
        if (kind === 'ticks') {
          const [x1, y1] = P(c, c, r, a), [x2, y2] = P(c, c, r + (major ? 13 : 7), a);
          el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)',
            'stroke-width': major ? 2 : 1, opacity: major ? 0.8 : 0.4 }, g);
        } else {
          const [x, y] = P(c, c, r, a);
          el('circle', { cx: x, cy: y, r: major ? 3.4 : 1.8,
            fill: major ? 'var(--hud-accent)' : 'var(--hud-cyan-45)',
            class: major ? 'hud-glow' : '' }, g);
        }
      }
    }
    spin(g, c, c, opts.dur || 90, opts.reverse);
    return s;
  };

  /* ============================================================
     WidgetFrame — an empty light-blue outline shaped to hold a
     widget. Each `shape` has a distinct silhouette so a deck of
     them doesn't read as a grid of identical boxes:
       notch  — chamfered top-left + bottom-right corners
       hex    — elongated hexagon, pointed left and right
       slant  — parallelogram leaning right
       tab    — header strip + one chamfered corner
       arrow  — right-pointing pentagon
       trapz  — trapezoid, narrow at top (echoes the floor)
     Returns a positioned <div>; append widgets into `._content`
     (also exposed as the .hud-content child).
     opts: {width, height, shape, role, cut}
     ============================================================ */
  HUD.widgetFrame = function (opts) {
    opts = opts || {};
    const W = opts.width || 300, H = opts.height || 160;
    const shape = opts.shape || 'notch';
    const k = opts.cut || 22;                 // chamfer / point depth
    const o = 1.5;                            // stroke inset
    const div = document.createElement('div');
    div.className = 'hud-widget';
    div.style.cssText = `position:relative;width:${W}px;height:${H}px`;
    if (opts.role) div.dataset.role = opts.role;

    const s = svg(W, H);
    s.style.cssText = 'position:absolute;left:0;top:0';
    const R = W - o, B = H - o;
    let d, pad = [14, 16];                    // [vertical, horizontal] content pad
    if (shape === 'banner') {                 // long top, 45deg descent right
      d = `M ${k} ${o} H ${R} L ${W - k * 1.8} ${B} H ${o} V ${k} Z`;
      pad = [16, k + 12];
    } else if (shape === 'hept') {            // irregular heptagon (plane panel)
      d = `M ${W * 0.28} ${o} H ${W * 0.76} L ${R} ${H * 0.30} V ${H * 0.72}
           L ${W * 0.72} ${B} H ${W * 0.24} L ${o} ${H * 0.66} V ${H * 0.28} Z`;
      pad = [20, 34];
    } else if (shape === 'bite') {            // chamfered rect, notch bitten out of the top
      d = `M ${k} ${o} H ${W * 0.38} l 7 9 h ${W * 0.14} l 7 -9 H ${W - k}
           L ${R} ${k} V ${B - k} L ${W - k} ${B} H ${k} L ${o} ${B - k} V ${k} Z`;
      pad = [22, 18];
    } else if (shape === 'keystone') {        // inverted trapezoid, wide at top
      d = `M ${o} ${o} H ${R} L ${W - k} ${B} H ${k} Z`;
      pad = [14, k + 12];
    } else if (shape === 'blade') {           // square right, pointed left nose
      d = `M ${k} ${o} H ${R} V ${B} H ${k * 1.8} L ${o} ${H * 0.5} Z`;
      pad = [14, k + 18];
    } else if (shape === 'stack') {           // stepped left edge
      d = `M ${k} ${o} H ${R} V ${B} H ${o} V ${H * 0.66} H ${k * 0.8}
           V ${H * 0.33} H ${k * 1.6} Z`;
      pad = [14, k + 16];
    } else if (shape === 'crest') {           // peaked top edge
      d = `M ${W / 2} ${o} L ${R} ${k * 1.6} V ${B} H ${o} V ${k * 1.6} Z`;
      pad = [k + 14, 16];
    } else if (shape === 'bracket') {         // corner brackets only, no outline
      d = null;
      pad = [16, 18];
    } else if (shape === 'hex') {
      d = `M ${k} ${o} H ${W - k} L ${R} ${H / 2} L ${W - k} ${B} H ${k} L ${o} ${H / 2} Z`;
      pad = [14, k + 10];
    } else if (shape === 'slant') {
      d = `M ${k} ${o} H ${R} L ${W - k} ${B} H ${o} Z`;
      pad = [14, k + 10];
    } else if (shape === 'tab') {
      d = `M ${o} ${o} H ${W - k} L ${R} ${k} V ${B} H ${o} Z`;
      pad = [30, 16];
    } else if (shape === 'arrow') {
      d = `M ${o} ${o} H ${W - k} L ${R} ${H / 2} L ${W - k} ${B} H ${o} Z`;
      pad = [14, 16];
    } else if (shape === 'trapz') {
      d = `M ${k} ${o} H ${W - k} L ${R} ${B} H ${o} Z`;
      pad = [14, k + 10];
    } else {                                   // notch
      d = `M ${k} ${o} H ${R} V ${H - k} L ${W - k} ${B} H ${o} V ${k} Z`;
    }
    if (d) {
      el('path', { d: d.replace(/\s+/g, ' '), fill: 'var(--hud-fill)',
        stroke: 'var(--hud-accent)', 'stroke-width': 1.3,
        'stroke-linejoin': 'miter', class: 'hud-glow' }, s);
    } else {
      // bracket: floating corner Ls over a bare fill, no continuous outline
      el('rect', { x: o, y: o, width: W - o * 2, height: H - o * 2,
        fill: 'var(--hud-fill)' }, s);
      const bl = 26;
      const gB = el('g', { class: 'hud-stroke-bright', 'stroke-width': 1.8,
        fill: 'none' }, s);
      [`M ${bl} ${o} H ${o} V ${bl}`, `M ${R - bl} ${o} H ${R} V ${bl}`,
       `M ${R - bl} ${B} H ${R} V ${B - bl}`, `M ${bl} ${B} H ${o} V ${B - bl}`]
        .forEach(p => el('path', { d: p }, gB));
    }
    if (shape === 'tab') {   // header divider under the tab strip
      el('line', { x1: o, y1: 22, x2: R, y2: 22, class: 'hud-stroke-dim',
        'stroke-width': 1 }, s);
    }
    if (shape === 'bite') {  // deco squares in the header band (ecg panel)
      [[14, 5], [30, 4]].forEach(([x, q]) =>
        el('rect', { x: k + x, y: 15, width: q * 2, height: q * 2,
          class: 'hud-stroke-faint', 'stroke-width': 1, fill: 'none' }, s));
    }
    // bright edge ticks so an empty frame still reads as instrumented
    if (['notch', 'hex', 'slant', 'tab', 'arrow', 'trapz', 'banner', 'blade'].indexOf(shape) !== -1) {
      const t = 12;
      el('path', { d: `M ${o} ${H / 2 - t} V ${H / 2 + t} M ${R} ${H / 2 - t} V ${H / 2 + t}`,
        class: 'hud-stroke-bright', 'stroke-width': 2 }, s);
    }
    div.appendChild(s);

    const content = document.createElement('div');
    content.className = 'hud-content';
    content.style.cssText = `position:absolute;left:${pad[1]}px;right:${pad[1]}px;` +
      `top:${pad[0]}px;bottom:${pad[0]}px;overflow:hidden`;
    div.appendChild(content);
    div._content = content;
    return div;
  };

  /* ============================================================
     FloorPlane — a trapezoid floor spanning the full bottom edge:
     narrow at the horizon, full width at the viewer. Filled with a
     navy → light-blue vertical gradient and overlaid with a light
     blue perspective grid (verticals converging on the vanishing
     point, horizontals bunching toward the horizon). The grid is
     clipped to the trapezoid so nothing leaks outside the plane.
     opts: {width=1600, height=380, topInset, vanishX, cols, rows, role}
     ============================================================ */
  let _floorSeq = 0;
  HUD.floorPlane = function (opts) {
    opts = opts || {};
    const W = opts.width || 1600, H = opts.height || 380;
    const inset = opts.topInset == null ? W * 0.30 : opts.topInset;
    const vp = opts.vanishX == null ? W / 2 : opts.vanishX;
    const uid = 'hud-floor-' + (++_floorSeq);
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;

    const shape = `M ${inset} 0 H ${W - inset} L ${W} ${H} H 0 Z`;
    const defs = el('defs', {}, s);
    // navy at the horizon → light blue toward the viewer
    const lg = el('linearGradient', { id: uid + '-g', x1: '0', y1: '0', x2: '0', y2: '1' }, defs);
    el('stop', { offset: '0%', 'stop-color': 'var(--hud-bg-edge)', 'stop-opacity': '0.85' }, lg);
    el('stop', { offset: '45%', 'stop-color': 'var(--hud-bg-center)', 'stop-opacity': '0.9' }, lg);
    el('stop', { offset: '100%', 'stop-color': 'var(--hud-cyan)', 'stop-opacity': '0.55' }, lg);
    const cp = el('clipPath', { id: uid + '-c' }, defs);
    el('path', { d: shape }, cp);

    el('path', { d: shape, fill: `url(#${uid}-g)` }, s);

    // grid, clipped to the plane
    const g = el('g', { 'clip-path': `url(#${uid}-c)`,
      stroke: 'var(--hud-accent)', fill: 'none' }, s);
    const cols = opts.cols || 19;
    for (let i = 0; i < cols; i++) {
      const t = (i - (cols - 1) / 2) / ((cols - 1) / 2);
      el('line', { x1: vp + t * (W / 2 - inset), y1: 0,
        x2: vp + t * W * 1.35, y2: H,
        'stroke-width': 1, opacity: 0.5 }, g);
    }
    const rows = opts.rows || 9;
    for (let i = 1; i <= rows; i++) {
      const t = i / rows, y = H * t * t;    // squared → bunched at the horizon
      el('line', { x1: 0, y1: y, x2: W, y2: y,
        'stroke-width': 1, opacity: 0.25 + 0.45 * t }, g);
    }
    // bright leading edge along the horizon
    el('path', { d: `M ${inset} 0 H ${W - inset}`, class: 'hud-stroke-bright',
      'stroke-width': 1.5 }, s);
    return s;
  };

  HUD.floorGrid = function (opts) {
    opts = opts || {};
    const W = opts.width || 1600, H = opts.height || 380;
    const vp = opts.vanishX == null ? W * 0.475 : opts.vanishX;
    const s = svg(W, H);
    // fade the floor out toward the horizon so it melts into the background
    s.style.maskImage = 'linear-gradient(180deg, transparent 0%, black 26%)';
    s.style.webkitMaskImage = 'linear-gradient(180deg, transparent 0%, black 26%)';
    const g = el('g', { stroke: 'var(--hud-blue)', 'stroke-width': 1 }, s);
    // fan verticals spreading wide from just above the horizon
    const n = opts.lines || 17;
    for (let i = 0; i < n; i++) {
      const t = (i - (n - 1) / 2) / ((n - 1) / 2);
      el('line', { x1: vp + t * W * 0.12, y1: 0,
        x2: vp + t * W * 1.15, y2: H, opacity: 0.4 }, g);
    }
    // horizontals: dense near the horizon, spreading toward the viewer
    for (let i = 1; i <= 8; i++) {
      const t = i / 8, y = H * t * t;
      el('line', { x1: 0, y1: y, x2: W, y2: y, opacity: 0.1 + 0.35 * t,
        'stroke-width': i > 6 ? 1.5 : 1 }, g);
    }
    // long bright crossing diagonals cutting the floor (ref)
    el('line', { x1: -30, y1: H * 0.9, x2: W * 0.62, y2: 4,
      class: 'hud-stroke-dim', 'stroke-width': 1.2 }, s);
    el('line', { x1: W + 30, y1: H * 0.82, x2: W * 0.45, y2: 2,
      class: 'hud-stroke-dim', 'stroke-width': 1.2 }, s);
    return s;
  };

  /* ============================================================
     FlowerOfLife — seed-of-life sacred-geometry medallion (ref
     right-lower panel centerpiece). opts: {r=26}
     ============================================================ */
  HUD.flowerOfLife = function (opts) {
    opts = opts || {};
    const R = opts.r || 26, S = R * 2 + 8, c = S / 2;
    const s = svg(S, S);
    const g = el('g', { class: 'hud-stroke', 'stroke-width': 1, fill: 'none' }, s);
    el('circle', { cx: c, cy: c, r: R, 'stroke-width': 1.3 }, g);
    const r2 = R / 2;
    el('circle', { cx: c, cy: c, r: r2 }, g);
    for (let i = 0; i < 6; i++) {
      const [x, y] = P(c, c, r2, i * 60);
      el('circle', { cx: x, cy: y, r: r2 }, g);
    }
    return s;
  };

  /* ============================================================
     CompassRing — thick dotted ring, long cross spokes running
     through everything, bright outer arc fragments, center dot
     (ref right-mid dial). opts: {size=160, role}
     ============================================================ */
  HUD.compassRing = function (opts) {
    opts = opts || {};
    const S = opts.size || 160, c = S / 2, r = S * 0.33;
    const s = svg(S, S, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    const gSpin = el('g', {}, s);
    el('circle', { cx: c, cy: c, r, class: 'hud-stroke', 'stroke-width': 3.5,
      'stroke-dasharray': '2.5 6' }, gSpin);
    spin(gSpin, c, c, 70);
    el('circle', { cx: c, cy: c, r: r * 0.6, class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    el('path', { d: `M ${-S * 0.06} ${c} H ${S * 1.06} M ${c} ${-S * 0.06} V ${S * 1.06}`,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    const frag = (a0, len) => {
      const [x0, y0] = P(c, c, r * 1.32, a0), [x1, y1] = P(c, c, r * 1.32, a0 + len);
      el('path', { d: `M ${x0} ${y0} A ${r * 1.32} ${r * 1.32} 0 0 1 ${x1} ${y1}`,
        fill: 'none', class: 'hud-stroke-bright', 'stroke-width': 4 }, s);
    };
    frag(300, 60); frag(120, 45);
    el('circle', { cx: c, cy: c, r: 4.5, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    return s;
  };

  /* ============================================================
     RadarFan — concentric dashed rings with a filled royal sweep
     wedge and a brighter inner wedge (ref rounded-panel radar).
     opts: {size=120}
     ============================================================ */
  HUD.radarFan = function (opts) {
    opts = opts || {};
    const S = opts.size || 120, c = S / 2;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    el('circle', { cx: c, cy: c, r: S * 0.44, class: 'hud-stroke-dim', 'stroke-width': 1.5,
      'stroke-dasharray': '3 4' }, s);
    el('circle', { cx: c, cy: c, r: S * 0.30, class: 'hud-stroke-faint', 'stroke-width': 1 }, s);
    const wedge = (a0, a1, r, fill, op) => {
      const [x0, y0] = P(c, c, r, a0), [x1, y1] = P(c, c, r, a1);
      el('path', { d: `M ${c} ${c} L ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1} Z`,
        fill, opacity: op }, s);
    };
    const gW = el('g', {}, s);
    const w2 = (a0, a1, r, fill, op) => {
      const [x0, y0] = P(c, c, r, a0), [x1, y1] = P(c, c, r, a1);
      el('path', { d: `M ${c} ${c} L ${x0} ${y0} A ${r} ${r} 0 0 1 ${x1} ${y1} Z`,
        fill, opacity: op }, gW);
    };
    w2(210, 285, S * 0.42, 'var(--hud-blue)', 0.85);
    w2(228, 270, S * 0.30, 'var(--hud-accent)', 0.5);
    spin(gW, c, c, 26);
    el('circle', { cx: c, cy: c, r: 3.5, fill: 'var(--hud-cyan)', class: 'hud-glow' }, s);
    return s;
  };

  /* ============================================================
     PlanePanel — irregular heptagon outline holding the faceted
     paper plane, 75% bracket label, mini bars, loader arc (ref
     bottom-right). opts: {width=295, height=190, role}
     ============================================================ */
  HUD.planePanel = function (opts) {
    opts = opts || {};
    const W = opts.width || 295, H = opts.height || 190;
    const s = svg(W, H, { style: 'overflow:visible' });
    if (opts.role) s.dataset.role = opts.role;
    el('path', { d: `M ${W * 0.30} 2 H ${W * 0.78} L ${W - 2} ${H * 0.30} V ${H * 0.72}
                     L ${W * 0.72} ${H - 2} H ${W * 0.26} L 2 ${H * 0.66} V ${H * 0.28} Z`,
      class: 'hud-stroke', 'stroke-width': 1.5, fill: 'var(--hud-fill)' }, s);
    // faceted paper plane: bright left face, royal right face, small keel
    const px = W * 0.56, py = H * 0.48;
    el('polygon', { points: `${px},${py - 56} ${px - 44},${py + 44} ${px - 6},${py + 20}`,
      fill: 'var(--hud-accent)', opacity: 0.9, class: 'hud-glow' }, s);
    el('polygon', { points: `${px},${py - 56} ${px + 40},${py + 48} ${px - 6},${py + 20}`,
      fill: 'var(--hud-blue)' }, s);
    el('polygon', { points: `${px - 6},${py + 20} ${px - 18},${py + 56} ${px + 2},${py + 32}`,
      fill: 'var(--hud-cyan)', opacity: 0.8 }, s);
    // 75% with bracket, mini bars, small bars, loader arc
    txt(el('text', { x: 26, y: 44, 'font-size': 16, class: 'hud-txt' }, s), '75%');
    el('path', { d: 'M 24 20 h 32 M 24 20 v 12', class: 'hud-stroke', 'stroke-width': 1.2,
      fill: 'none' }, s);
    [[0, 14], [1, 20], [2, 9]].forEach(([i, h]) =>
      el('rect', { x: W - 48 + i * 10, y: 36 - h, width: 6, height: h,
        fill: 'var(--hud-cyan)', opacity: 0.85 }, s));
    [[0, 10], [1, 16], [2, 7]].forEach(([i, h]) =>
      el('rect', { x: 30 + i * 8, y: H - 36 + (16 - h), width: 5, height: h,
        fill: 'var(--hud-blue)' }, s));
    const [lx0, ly0] = P(W * 0.44, H - 30, 12, 40), [lx1, ly1] = P(W * 0.44, H - 30, 12, 320);
    el('path', { d: `M ${lx0} ${ly0} A 12 12 0 1 1 ${lx1} ${ly1}`, fill: 'none',
      class: 'hud-stroke-bright', 'stroke-width': 3 }, s);
    return s;
  };

  /* ============================================================
     BatteryBar — horizontal battery capsule with end cap, music
     note icon, white % value (ref bottom-right).
     opts: {width=210, height=54, value='100%', role}
     ============================================================ */
  HUD.batteryBar = function (opts) {
    opts = opts || {};
    const W = opts.width || 210, H = opts.height || 54;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    el('path', { d: 'M 14 34 V 12 l 10 -4 V 30', class: 'hud-stroke', 'stroke-width': 2,
      fill: 'none' }, s);
    el('circle', { cx: 11, cy: 35, r: 4, fill: 'var(--hud-cyan)' }, s);
    el('circle', { cx: 21, cy: 31, r: 4, fill: 'var(--hud-cyan)' }, s);
    el('rect', { x: 34, y: 8, width: W - 60, height: H - 16, class: 'hud-stroke',
      'stroke-width': 1.5, fill: 'var(--hud-fill)' }, s);
    el('rect', { x: 40, y: 14, width: (W - 72) * 0.97, height: H - 28,
      fill: 'var(--hud-cyan-25)' }, s);
    el('rect', { x: W - 22, y: 16, width: 6, height: H - 32, fill: 'var(--hud-cyan)' }, s);
    txt(el('text', { x: 34 + (W - 60) / 2, y: H / 2 + 7, 'text-anchor': 'middle',
      'font-size': 18, class: 'hud-txt' }, s), opts.value || '100%');
    return s;
  };

  /* ============================================================
     NodeDot — small bright filled circle used as a link node
     between elements (ref: dots between the 30/50/70 gauges).
     opts: {r=4}
     ============================================================ */
  HUD.nodeDot = function (opts) {
    opts = opts || {};
    const r = opts.r || 4, S = r * 2 + 4;
    const s = svg(S, S);
    el('circle', { cx: S / 2, cy: S / 2, r, fill: 'var(--hud-cyan)', class: 'hud-glow' }, s);
    return s;
  };

  /* ============================================================
     Decorative primitives — the target's "filler language".
     Small, reusable; scatter them to hit HUD-grade density.
     ============================================================ */

  // hexagon cluster (honeycomb), some cells filled
  HUD.hexCluster = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 2, cols = opts.cols || 5, R = opts.r || 13;
    const W = cols * R * 1.74 + R, H = rows * R * 1.5 + R * 1.2;
    const s = svg(W, H);
    for (let r = 0; r < rows; r++) for (let cc = 0; cc < cols; cc++) {
      const cx = R + cc * (R * 1.74) + (r % 2 ? R * 0.87 : 0);
      const cy = R + r * (R * 1.5);
      let d = '';
      for (let i = 0; i < 6; i++) {
        const a = Math.PI / 3 * i + Math.PI / 6;
        d += (i ? 'L' : 'M') + (cx + R * Math.cos(a)) + ' ' + (cy + R * Math.sin(a)) + ' ';
      }
      const filled = (r * cols + cc) % 3 === 0;
      el('path', { d: d + 'Z', fill: filled ? 'var(--hud-cyan-25)' : 'none',
        class: 'hud-stroke', 'stroke-width': 1 }, s);
    }
    return s;
  };

  // flowing chevron run (>>>>>)
  HUD.chevronRun = function (opts) {
    opts = opts || {};
    const n = opts.count || 8, step = opts.step || 24, h = opts.h || 26;
    const W = n * step + 10, H = h + 8;
    const s = svg(W, H);
    const chevs = [];
    for (let i = 0; i < n; i++) {
      const x = 4 + i * step;
      chevs.push(el('path', { d: `M ${x} 4 l ${h * 0.55} ${h / 2} l ${-h * 0.55} ${h / 2}`,
        class: 'hud-stroke-bright', 'stroke-width': 4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: 0.3 }, s));
    }
    if (gsap) gsap.to(chevs, { opacity: 1, duration: 0.5, ease: 'sine.inOut',
      stagger: { each: 0.12, repeat: -1, yoyo: true } });
    return s;
  };

  // horizontal tick / dash strip
  HUD.tickStrip = function (opts) {
    opts = opts || {};
    const n = opts.count || 16, step = opts.step || 13, h = opts.h || 12;
    const W = n * step + 6, H = h + 6;
    const s = svg(W, H);
    for (let i = 0; i < n; i++) {
      const major = i % 5 === 0;
      el('line', { x1: 3 + i * step, y1: 3, x2: 3 + i * step, y2: 3 + (major ? h : h * 0.6),
        stroke: major ? 'var(--hud-accent)' : 'var(--hud-cyan-45)', 'stroke-width': major ? 2 : 1,
        class: major ? 'hud-glow' : '' }, s);
    }
    return s;
  };

  // dotted matrix of small squares (varying opacity → "data noise")
  // gapCols: column indices left fully blank, splitting the matrix into
  // clustered blocks like the reference's top-left data field.
  HUD.dotMatrix = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 6, cols = opts.cols || 24, cell = opts.cell || 11, sz = opts.size || 6;
    const gaps = opts.gapCols || [];
    const s = svg(cols * cell, rows * cell);
    for (let r = 0; r < rows; r++) for (let cc = 0; cc < cols; cc++) {
      if (gaps.indexOf(cc) !== -1) continue;
      const v = (r * 31 + cc * 7) % 11;
      if (v > 8) continue;              // empty cells — the ref matrix breathes
      const rect = el('rect', { x: cc * cell, y: r * cell, width: sz, height: sz,
        fill: v < 3 ? 'var(--hud-accent)' : v < 6 ? 'var(--hud-cyan-45)' : 'var(--hud-cyan-12)' }, s);
      // fade: columns dim as the matrix reaches toward the reactor
      if (opts.fade) rect.setAttribute('opacity', (1 - 0.82 * (cc / (cols - 1))).toFixed(2));
    }
    return s;
  };

  // a row of small donut rings with partial fills
  HUD.miniRings = function (opts) {
    opts = opts || {};
    const specs = opts.rings || [[14, 0.8], [17, 0.5], [15, 0.7], [19, 0.4], [13, 0.85]];
    const gap = opts.gap || 16;
    let W = 0; specs.forEach(([r]) => W += r * 2 + gap); const maxR = Math.max.apply(null, specs.map(x => x[0]));
    const H = maxR * 2 + 8;
    const s = svg(W, H);
    let x = 4;
    specs.forEach(([r, f]) => {
      const cx = x + r, cy = maxR + 4, C = 2 * Math.PI * r;
      const sw = opts.strokeWidth || 5;
      el('circle', { cx, cy, r, fill: 'none', stroke: 'var(--hud-cyan-25)', 'stroke-width': sw }, s);
      el('circle', { cx, cy, r, fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': sw,
        'stroke-dasharray': `${C * f} ${C}`, transform: `rotate(-90 ${cx} ${cy})`, class: 'hud-glow' }, s);
      if (opts.centerDot !== false)
        el('circle', { cx, cy, r: 3, fill: 'var(--hud-white)' }, s);
      if (opts.endDot) {
        // small bright dot where the value arc ends (broken-ring style)
        const [ex, ey] = P(cx, cy, r, f * 360);
        el('circle', { cx: ex, cy: ey, r: sw * 0.7, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
      }
      x += r * 2 + gap;
    });
    return s;
  };

  // small "+" crosshair marker with soft blink
  HUD.marker = function (opts) {
    opts = opts || {};
    const S = opts.size || 18;
    const s = svg(S, S);
    const c = S / 2, a = S * 0.4;
    el('path', { d: `M ${c} ${c - a} V ${c + a} M ${c - a} ${c} H ${c + a}`,
      class: 'hud-stroke-bright', 'stroke-width': 1.5 }, s);
    if (gsap) gsap.to(s, { opacity: 0.35, duration: 1.5, ease: 'sine.inOut', repeat: -1, yoyo: true });
    return s;
  };

  /* ============================================================
     TOP-MIDDLE BANNER STRIP components (actual-ref pass 2)
     ============================================================ */

  /* BannerFrame — the blade-shaped banner: long bottom edge, 45deg
     left slant up to a short top edge, then one long shallow diagonal
     descending to an acute tip at bottom-right (play triangle nests
     in the tip). Full inner offset outline + node dots at TL corner,
     mid-slant, mid-diagonal, near-tip and BL corner.
     opts: {width=560, height=158, tlOff=95, topEnd=355} */
  HUD.bannerFrame = function (opts) {
    opts = opts || {};
    const W = opts.width || 560, H = opts.height || 158;
    const tl = opts.tlOff == null ? 95 : opts.tlOff;
    const te = opts.topEnd == null ? 355 : opts.topEnd;
    const s = svg(W, H, { style: 'overflow: visible' });
    if (opts.role) s.dataset.role = opts.role;
    // ONE straight 45deg descent from the top edge to the tip; the inner
    // accent line descends in parallel leaving a dark corridor between.
    const dy = H - 2, tipX = te + dy, tipY = H - 1;
    // faint interior wash (closed path, fill only)
    el('path', { d: `M 0 ${tipY} L ${tl} 1 H ${te} L ${tipX} ${tipY} Z`,
      fill: 'var(--hud-fill)', stroke: 'none' }, s);
    // open strokes with OVERSHOT corners (ref: lines cross past every joint)
    const og = el('g', { class: 'hud-stroke', 'stroke-width': 1.6, fill: 'none' }, s);
    // left slant overshoots past both the bottom corner and the top edge
    el('line', { x1: -14, y1: H + 12, x2: tl + 17, y2: -16 }, og);
    // top edge overshoots left of the slant; its tip is an open 45deg chamfer
    el('path', { d: `M ${tl - 81} 17 L ${tl - 65} 1 H ${te}` }, og);
    // descent to the tip + bottom edge overshooting left past the corner
    el('path', { d: `M ${te} 1 L ${tipX} ${tipY} M -26 ${tipY} H ${tipX}` }, og);
    // 45deg stub off the top-left corner descending into the interior, fdot end
    el('path', { d: `M ${tl - 10} 1 L ${tl + 26} 37`, class: 'hud-stroke',
      'stroke-width': 1.2, fill: 'none' }, s);
    el('circle', { cx: tl + 26, cy: 37, r: 4, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    // fdot resting on the bottom edge right of the corner
    el('circle', { cx: 103, cy: tipY, r: 4.5, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    // doubled bottom edge (dim)
    el('line', { x1: 26, y1: H - 11, x2: tipX - 34, y2: H - 11,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    // inner accent: start fdot, run under the top edge, then descend 45deg
    // parallel inside the outer descent; its far end terminates at the
    // broken-ring node the template places there (no end dot of its own).
    const ax0 = te * 0.59, ay = 26;
    el('path', { d: `M ${ax0} ${ay} H ${te - 23} L ${te + 52} ${ay + 75}`,
      class: 'hud-stroke', 'stroke-width': 1.2, fill: 'none' }, s);
    el('circle', { cx: ax0, cy: ay, r: 4, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    // dim doubled top-edge segment on the left third (ref doubles it there)
    el('line', { x1: tl, y1: 10, x2: ax0, y2: 10,
      class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    // node dots: BL, TL, mid-slant, and mid-descent
    [[0, tipY], [tl, 1], [tl / 2, H / 2], [te + dy * 0.45, 1 + dy * 0.45]]
      .forEach(([cx, cy]) =>
        el('circle', { cx, cy, r: 4.5, fill: 'var(--hud-accent)', class: 'hud-glow' }, s));
    return s;
  };

  /* FanRosette — chaotic scribble rosette with outlined wedge fan
     blades (the banner's left ornament in the actual ref).
     opts: {size=130, role} */
  HUD.fanRosette = function (opts) {
    opts = opts || {};
    const S = opts.size || 130, c = S / 2, r = S * 0.48;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    const g = el('g', {}, s);
    const rad = Math.PI / 180;
    // outlined annular fan blades
    [[-52, -10, 0.50, 0.96], [8, 46, 0.42, 0.88], [150, 196, 0.48, 0.92]]
      .forEach(([a0, a1, ki, ko]) => {
        el('path', { d: d3.arc()({ startAngle: a0 * rad, endAngle: a1 * rad,
          innerRadius: r * ki, outerRadius: r * ko }),
          transform: `translate(${c} ${c})`, class: 'hud-stroke',
          'stroke-width': 1.2, fill: 'rgba(79, 212, 232, 0.08)' }, g);
      });
    // irregular scribble rings
    [[0.90, '3 9', 0], [0.72, '14 22', 40], [0.55, '6 5', -30],
     [0.38, '18 9', 80], [0.25, '4 7', 10]].forEach(([k, dash, rot]) => {
      el('circle', { cx: c, cy: c, r: r * k, class: 'hud-stroke-faint',
        'stroke-width': 1, 'stroke-dasharray': dash,
        transform: `rotate(${rot} ${c} ${c})` }, g);
    });
    // inner chaotic ellipses
    [[0.55, 0.20, 25], [0.40, 0.14, -40], [0.30, 0.24, 70]].forEach(([kx, ky, rot]) => {
      el('ellipse', { cx: c, cy: c, rx: r * kx, ry: r * ky,
        transform: `rotate(${rot} ${c} ${c})`, class: 'hud-stroke-faint',
        'stroke-width': 1 }, g);
    });
    el('circle', { cx: c, cy: c, r: 2.5, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    spin(g, c, c, 52);
    return s;
  };

  /* WireSphere — chaotic wireframe gyroscope: tilted ellipses +
     partial arcs, slow counter-rotation. opts: {size=130, role} */
  HUD.wireSphere = function (opts) {
    opts = opts || {};
    const S = opts.size || 130, c = S / 2, r = S * 0.44;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    const g1 = el('g', {}, s), g2 = el('g', {}, s);
    // tilted ellipse shells
    [[0.98, 0.30, 18], [0.92, 0.52, -24], [0.85, 0.72, 55], [0.99, 0.16, -62],
     [0.74, 0.95, 8], [1.22, 0.18, -8]].forEach(([kx, ky, rot], i) => {
      el('ellipse', { cx: c, cy: c, rx: r * kx, ry: r * ky,
        transform: `rotate(${rot} ${c} ${c})`,
        class: i % 2 ? 'hud-stroke-dim' : 'hud-stroke', 'stroke-width': 1 }, i % 2 ? g2 : g1);
    });
    // scribble arcs (dashed partial circles at odd radii)
    [[0.62, '30 14', 40], [0.34, '10 18', -70], [0.5, '52 30', 130]].forEach(([k, dash, rot]) => {
      el('circle', { cx: c, cy: c, r: r * k, class: 'hud-stroke-faint',
        'stroke-width': 1, 'stroke-dasharray': dash,
        transform: `rotate(${rot} ${c} ${c})` }, g2);
    });
    el('circle', { cx: c, cy: c, r: 2.5, fill: 'var(--hud-accent)', class: 'hud-glow' }, s);
    spin(g1, c, c, 44); spin(g2, c, c, 58, true);
    return s;
  };

  /* MirrorBars — vertical spine with mirrored horizontal strokes of
     varying width (the banner's central data column).
     opts: {rows=16, width=110, height=96} */
  HUD.mirrorBars = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 16, W = opts.width || 110, H = opts.height || 96;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const cx = W / 2, step = H / rows;
    // spine overshoots the rows top and bottom (ref's I-beam look)
    el('line', { x1: cx, y1: -8, x2: cx, y2: H + 8, class: 'hud-stroke', 'stroke-width': 1.5 }, s);
    // seeded half-width pairs [left, right] as fractions of W/2, 0 = skip side
    const seed = [[0.9, 0.86], [0.55, 0.6], [0.98, 0.3], [0.42, 0.95], [0.75, 0.7],
                  [0.2, 0.5], [0.88, 0.92], [0.35, 0.25], [0.6, 0.82], [0.95, 0.5],
                  [0.5, 0.4], [0.8, 0.9], [0.3, 0.68], [0.92, 0.35], [0.65, 0.75], [0.45, 0.55]];
    for (let i = 0; i < rows; i++) {
      const [lw, rw] = seed[i % seed.length], y = step * (i + 0.5);
      const bright = i % 3 !== 1;
      el('line', { x1: cx - (W / 2) * lw, y1: y, x2: cx + (W / 2) * rw, y2: y,
        stroke: bright ? 'var(--hud-cyan)' : 'var(--hud-blue)',
        'stroke-width': Math.max(2, step * 0.42),
        class: bright ? 'hud-glow' : '', opacity: bright ? 0.9 : 0.75 }, s);
    }
    return s;
  };

  /* CircleGrid — small open status circles, some drawn with a gap.
     opts: {rows=2, cols=3, r=8, gap=14} */
  HUD.circleGrid = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 2, cols = opts.cols || 3, r = opts.r || 8;
    const gap = opts.gap || 14;
    const W = cols * (r * 2 + gap), H = rows * (r * 2 + gap);
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const frac = [1, 0.72, 0.85, 0.6, 1, 0.78];
    for (let i = 0; i < rows * cols; i++) {
      const cx = (i % cols) * (r * 2 + gap) + r + gap / 2;
      const cy = Math.floor(i / cols) * (r * 2 + gap) + r + gap / 2;
      const C = 2 * Math.PI * r, f = frac[i % frac.length];
      el('circle', { cx, cy, r, fill: 'none', class: 'hud-stroke', 'stroke-width': 1.6,
        'stroke-dasharray': f === 1 ? 'none' : `${C * f} ${C}`,
        transform: `rotate(${(i * 77) % 360} ${cx} ${cy})` }, s);
    }
    return s;
  };

  /* SolidTriangle — filled accent play-triangle. dir: 'right'|'left'|'up' */
  HUD.solidTriangle = function (opts) {
    opts = opts || {};
    const S = opts.size || 44, h = S * 0.86;
    const s = svg(S, S);
    if (opts.role) s.dataset.role = opts.role;
    const pts = { right: `1,${(S - h) / 2} 1,${(S + h) / 2} ${S - 1},${S / 2}`,
                  left: `${S - 1},${(S - h) / 2} ${S - 1},${(S + h) / 2} 1,${S / 2}`,
                  up: `${(S - h) / 2},${S - 1} ${(S + h) / 2},${S - 1} ${S / 2},1`,
                  // 45deg right wedge: apex top-RIGHT, hypotenuse ascending
                  // left-to-right — mirrors a banner descent to form the
                  // arrowhead at the tip; vertical right leg, flat bottom
                  wedge: `${S - 1},1 1,${S - 1} ${S - 1},${S - 1}` };
    el('polygon', { points: pts[opts.dir || 'right'],
      fill: 'var(--hud-accent)', class: 'hud-glow-strong' }, s);
    return s;
  };

  /* PillOutline — stadium (capsule) outline, optionally dashed.
     opts: {width=150, height=36, dash} */
  HUD.pillOutline = function (opts) {
    opts = opts || {};
    const W = opts.width || 150, H = opts.height || 36;
    const s = svg(W, H);
    if (opts.role) s.dataset.role = opts.role;
    const a = { x: 1, y: 1, width: W - 2, height: H - 2, rx: (H - 2) / 2,
      class: 'hud-stroke', 'stroke-width': 1.4 };
    if (opts.dash) a['stroke-dasharray'] = opts.dash;
    el('rect', a, s);
    if (opts.inner) {
      const i = opts.inner === true ? 6 : opts.inner;
      el('rect', { x: 1 + i, y: 1 + i, width: W - 2 - i * 2, height: H - 2 - i * 2,
        rx: (H - 2 - i * 2) / 2, class: 'hud-stroke-dim', 'stroke-width': 1 }, s);
    }
    return s;
  };

  /* Waffle — slanted panel filled with a grid of small squares and a
     solid triangle in the lower-right (actual-ref right of banner).
     opts: {rows=5, cols=9, cell=10, skew=-14} */
  HUD.waffle = function (opts) {
    opts = opts || {};
    const rows = opts.rows || 5, cols = opts.cols || 9, cell = opts.cell || 10;
    const skew = opts.skew == null ? -14 : opts.skew;
    const gw = cols * cell, gh = rows * cell;
    const pad = 8, W = gw + pad * 2 + Math.abs(skew) * 2, H = gh + pad * 2;
    const s = svg(W, H, { style: 'overflow: visible' });
    if (opts.role) s.dataset.role = opts.role;
    const g = el('g', { transform: `skewX(${skew})`,
      'transform-origin': `${W / 2} ${H / 2}` }, s);
    el('rect', { x: Math.abs(skew), y: 1, width: gw + pad * 2, height: H - 2,
      class: 'hud-stroke', 'stroke-width': 1.2, fill: 'var(--hud-fill)' }, g);
    for (let rI = 0; rI < rows; rI++)
      for (let cI = 0; cI < cols; cI++)
        el('rect', { x: Math.abs(skew) + pad + cI * cell + 1, y: pad + rI * cell + 1,
          width: cell - 3, height: cell - 3, fill: 'var(--hud-cyan)',
          opacity: 0.55 + ((rI * cols + cI) % 4) * 0.12 }, g);
    return s;
  };

  global.HUD = HUD;
})(window);
