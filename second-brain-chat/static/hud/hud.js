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

    // outer slow ring: ticks + dot ring
    const gSlow = el('g', {}, s);
    el('circle', { cx: c, cy: c, r: R(238), class: 'hud-stroke-dim', 'stroke-width': 1 }, gSlow);
    for (let i = 0; i < 72; i++) {
      const major = i % 6 === 0;
      const [x1, y1] = P(c, c, R(major ? 222 : 228), i / 72 * 360);
      const [x2, y2] = P(c, c, R(238), i / 72 * 360);
      el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan)',
        'stroke-width': major ? 2 : 1, opacity: major ? 0.85 : 0.4 }, gSlow);
    }
    for (let i = 0; i < 48; i++) {
      const [dx, dy] = P(c, c, R(208), i / 48 * 360);
      el('circle', { cx: dx, cy: dy, r: 2, fill: i % 4 ? 'var(--hud-cyan-45)' : 'var(--hud-accent)' }, gSlow);
    }
    spin(gSlow, c, c, 150);

    // blade ring
    const gBlades = el('g', { class: 'hud-glow' }, s);
    const jit = [0, 3, -3, 2, 4, -2, 3, -4, 2, -3, 4, -2, 3, -3, 2, -4];
    for (let i = 0; i < 16; i++) {
      const fam = i % 3;
      const r0 = R(fam === 0 ? 200 : fam === 1 ? 190 : 178);
      const r1 = R(fam === 0 ? 154 : fam === 1 ? 148 : 154);
      const w0 = R(fam === 0 ? 20 : fam === 1 ? 17 : 13);
      const w1 = R(fam === 0 ? 14 : fam === 1 ? 11 : 9);
      const bright = i % 5 === 0;
      el('path', {
        d: `M ${-w0} ${-r0} L ${w0} ${-r0} L ${w1} ${-r1} L ${-w1} ${-r1} Z`,
        fill: bright ? 'rgba(155,242,250,0.28)' : 'rgba(79,212,232,0.12)',
        stroke: bright ? 'var(--hud-accent)' : 'var(--hud-cyan)',
        'stroke-width': bright ? 1.5 : 1,
        transform: `rotate(${i * 22.5 + jit[i]} ${c} ${c}) translate(${c} ${c})`
      }, gBlades);
    }
    spin(gBlades, c, c, 100, true);

    // mid ring: dashed rings + micro segments
    const gMid = el('g', {}, s);
    el('circle', { cx: c, cy: c, r: R(150), class: 'hud-stroke', 'stroke-width': 1.5, 'stroke-dasharray': '58 12 6 12' }, gMid);
    el('circle', { cx: c, cy: c, r: R(128), class: 'hud-stroke-dim', 'stroke-width': 1, 'stroke-dasharray': '2 7' }, gMid);
    el('circle', { cx: c, cy: c, r: R(246), class: 'hud-stroke-faint', 'stroke-width': 1 }, gMid);
    for (let i = 0; i < 24; i++) {
      const a0 = i / 24 * 360 + 2, a1 = a0 + 11;
      const [x0, y0] = P(c, c, R(140), a0);
      const [x1, y1] = P(c, c, R(140), a1);
      el('path', { d: `M ${x0} ${y0} A ${R(140)} ${R(140)} 0 0 1 ${x1} ${y1}`,
        fill: 'none', stroke: i % 2 ? 'var(--hud-cyan)' : 'var(--hud-cyan-25)', 'stroke-width': R(5) }, gMid);
    }
    spin(gMid, c, c, 64);

    // micro spokes
    const gMicro = el('g', {}, s);
    for (let i = 0; i < 12; i++) {
      const [x1, y1] = P(c, c, R(92), i / 12 * 360);
      const [x2, y2] = P(c, c, R(116), i / 12 * 360);
      el('line', { x1, y1, x2, y2, stroke: 'var(--hud-cyan-45)', 'stroke-width': 2 }, gMicro);
    }
    spin(gMicro, c, c, 30);

    // inner bright arcs
    const gInner = el('g', {}, s);
    const gb = el('g', { class: 'hud-stroke-bright', 'stroke-width': R(7) }, gInner);
    el('path', { d: `M ${c} ${c - R(98)} A ${R(98)} ${R(98)} 0 0 1 ${c + R(85)} ${c - R(49)}`, opacity: 0.85 }, gb);
    el('path', { d: `M ${c + R(91)} ${c + R(46)} A ${R(98)} ${R(98)} 0 0 1 ${c + R(10)} ${c + R(98)}`, opacity: 0.5 }, gb);
    el('path', { d: `M ${c - R(57)} ${c + R(77)} A ${R(98)} ${R(98)} 0 0 1 ${c - R(97)} ${c + R(8)}`, opacity: 0.7 }, gb);
    el('circle', { cx: c, cy: c, r: R(80), class: 'hud-stroke-dim', 'stroke-width': 1, 'stroke-dasharray': '1 5' }, gInner);
    el('circle', { cx: c, cy: c, r: R(64), class: 'hud-stroke', 'stroke-width': 1, 'stroke-dasharray': '14 5 3 5', opacity: 0.7 }, gInner);
    spin(gInner, c, c, 40, true);

    // diagonal pointer arrows
    const gArrow = el('g', { class: 'hud-stroke', 'stroke-width': 1.5 }, s);
    [[1, 1, -1], [-1, 1, 1], [1, -1, -1], [-1, -1, 1]].forEach(([sx, sy]) => {
      const bx = c + sx * R(174), by = c - sy * R(174);
      const tx = bx + sx * R(34), ty = by - sy * R(34);
      el('path', { d: `M ${bx} ${by} L ${tx} ${ty} M ${tx} ${ty} l ${-sx * 12} ${sy * 2} M ${tx} ${ty} l ${-sx * 2} ${sy * 12}` }, gArrow);
    });

    // glowing core + crosshair
    const gCore = el('g', { class: 'hud-glow-core' }, s);
    el('circle', { cx: c, cy: c, r: R(58), fill: `url(#${gid})` }, gCore);
    el('circle', { cx: c, cy: c, r: R(36), class: 'hud-stroke', 'stroke-width': 1, opacity: 0.75 }, gCore);
    el('circle', { cx: c, cy: c, r: R(11), fill: '#ffffff' }, gCore);
    el('path', { d: `M ${c} ${c - R(28)} V ${c - R(44)} M ${c} ${c + R(28)} V ${c + R(44)} M ${c - R(28)} ${c} H ${c - R(44)} M ${c + R(28)} ${c} H ${c + R(44)}`,
      class: 'hud-stroke-bright', 'stroke-width': 1.5, opacity: 0.9 }, gCore);
    if (gsap) gsap.to(gCore, { scale: 1.05, opacity: 0.94, svgOrigin: `${c} ${c}`,
      duration: 1.7, ease: 'sine.inOut', repeat: -1, yoyo: true });

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
    const midY = H * 0.55;
    // build a repeating EKG path across the width
    let d = `M 6 ${midY}`;
    const beat = [[16, 0], [8, -6], [8, 12], [6, -22], [8, 26], [6, -14], [6, 5]];
    let x = 6;
    while (x < W - 30) {
      d += ` h 22`; x += 22;
      for (const [dx, dy] of beat) { d += ` l ${dx} ${dy}`; x += dx; }
    }
    d += ` H ${W - 6}`;
    el('path', { d, class: 'hud-stroke', 'stroke-width': 2, opacity: 0.55, 'stroke-linejoin': 'round' }, s);
    const sweep = el('path', { d, class: 'hud-stroke-bright', 'stroke-width': 2, 'stroke-linejoin': 'round',
      'stroke-dasharray': '70 1400' }, s);
    if (gsap) gsap.fromTo(sweep, { strokeDashoffset: 1470 }, { strokeDashoffset: 0, duration: 3.4, ease: 'none', repeat: -1 });
    if (opts.label) txt(el('text', { x: 6, y: 14, 'font-size': 9, class: 'hud-txt-dim' }, s), opts.label);
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
          if (startStyle === 'square')
            el('rect', { x: start.x - 2.4, y: start.y - 2.4, width: 4.8, height: 4.8, fill: 'var(--hud-accent)' }, g);
          else if (startStyle === 'circle')
            el('circle', { cx: start.x, cy: start.y, r: 3.2, fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': 1.2 }, g);
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
    const end = 2 * Math.PI * (val / 100);
    const g = el('g', { transform: `translate(${c} ${c})`, class: 'hud-glow' }, s);
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
      const h0 = 6 + (seed[i % seed.length] / 46) * (H - 6);
      const r = el('rect', { x: ext + i * (bw + gap), y: H - h0, width: bw, height: h0,
        fill: tone(i), class: opts.mixed && i % 5 === 2 ? '' : 'hud-glow' }, s);
      if (gsap) {
        const h1 = 6 + Math.max(5, (H - 10) * (((i * 13) % 9) / 9));
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
      class: 'hud-stroke', 'stroke-width': 1.2, 'stroke-dasharray': opts.dash || '5 4' }, s);
    if (corner === 'brackets') {
      const bl = opts.bracket || Math.min(W, H) * 0.22, o = 1;
      const g = el('g', { class: 'hud-stroke-bright', 'stroke-width': 1.5,
        'stroke-linecap': 'round' }, s);
      [`M ${o} ${bl} V ${o} H ${bl}`, `M ${W - bl} ${o} H ${W - o} V ${bl}`,
       `M ${W - o} ${H - bl} V ${H - o} H ${W - bl}`, `M ${bl} ${H - o} H ${o} V ${H - bl}`]
        .forEach(d => el('path', { d }, g));
    } else if (corner === 'squares') {
      // solid squares centered on the dashed rect's corners (actual-ref style)
      const q = Math.min(W, H) * 0.18;
      [[inset, inset], [W - inset, inset], [inset, H - inset], [W - inset, H - inset]]
        .forEach(([cx, cy]) => el('rect', { x: cx - q / 2, y: cy - q / 2, width: q, height: q,
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
  HUD.floorGrid = function (opts) {
    opts = opts || {};
    const W = opts.width || 1600, H = opts.height || 170;
    const vp = opts.vanishX == null ? W / 2 : opts.vanishX;
    const s = svg(W, H);
    // fade the strip out toward its top so it melts into the background
    s.style.maskImage = 'linear-gradient(180deg, transparent 0%, black 55%)';
    s.style.webkitMaskImage = 'linear-gradient(180deg, transparent 0%, black 55%)';
    const g = el('g', { stroke: 'var(--hud-cyan-25)', 'stroke-width': 1 }, s);
    const n = 23;
    for (let i = 0; i < n; i++) {
      const t = i - (n - 1) / 2;
      el('line', { x1: vp + t * (W * 0.62 / n), y1: 0,
        x2: vp + t * (W * 1.55 / n), y2: H, opacity: 0.45 }, g);
    }
    [0.08, 0.20, 0.35, 0.53, 0.73, 0.92].forEach(f =>
      el('line', { x1: 0, y1: H * f, x2: W, y2: H * f, opacity: 0.12 + 0.4 * f }, g));
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
      el('circle', { cx, cy, r, fill: 'none', stroke: 'var(--hud-cyan-25)', 'stroke-width': 5 }, s);
      el('circle', { cx, cy, r, fill: 'none', stroke: 'var(--hud-cyan)', 'stroke-width': 5,
        'stroke-dasharray': `${C * f} ${C}`, transform: `rotate(-90 ${cx} ${cy})`, class: 'hud-glow' }, s);
      el('circle', { cx, cy, r: 3, fill: 'var(--hud-white)' }, s);
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

  global.HUD = HUD;
})(window);
