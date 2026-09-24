/* Intention — the phone app. Vanilla JS, no build step.
   Reads the day from /api/day (built live from the training app), shows NOW / NEXT /
   the timeline, and the Done tap is the log. */
(() => {
  const VERSION = 10;
  const $app = document.getElementById('app');
  const LS = { key: 'intention.key', subscribed: 'intention.subscribed', cache: 'intention.daycache' };
  const GOAL_LABEL = { HEALTH: 'General health', BASKETBALL: 'Basketball excellence', CLASSROOM: 'Classroom excellence', MONEY: 'Money making' };
  const GOALS = ['HEALTH', 'BASKETBALL', 'CLASSROOM', 'MONEY'];
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const st = { key: localStorage.getItem(LS.key) || '', day: null, date: null, today: null, err: '', expanded: {}, msg: '', tools: {}, editing: false, settings: null, notif: {} };

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtDate = (iso) => { const [y, m, d] = iso.split('-').map(Number); const dt = new Date(Date.UTC(y, m - 1, d)); return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric', timeZone: 'UTC' }); };
  const nowMs = () => Date.now();
  function route() {
    const h = location.hash.replace(/^#\/?/, '');
    const [path, q] = h.split('?');
    const params = new URLSearchParams(q || '');
    const parts = path.split('/').filter(Boolean);
    if (parts[0] === 'item') return { view: 'item', id: decodeURIComponent(parts.slice(1).join('/')), date: params.get('d') };
    if (parts[0] === 'settings') return { view: 'settings' };
    if (parts[0] === 'day') return { view: 'home', date: parts[1] };
    return { view: 'home' };
  }
  const go = (h) => { location.hash = h; };
  async function api(path, method = 'GET', body) {
    const r = await fetch('/api' + path, { method, headers: { 'authorization': 'Bearer ' + st.key, ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
    if (r.status === 401) { st.err = 'unauthorized'; throw new Error('unauthorized'); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }

  // ---------------------------------------------------------------- data
  async function loadDay(date) {
    const want = date || st.date || null;
    try {
      const d = await api('/day' + (want ? '?date=' + want : ''));
      st.day = d; st.date = d.date; st.today = d.today; st.err = '';
      try { localStorage.setItem(LS.cache, JSON.stringify(d)); } catch {}
    } catch (e) {
      if (!st.day) { try { const c = JSON.parse(localStorage.getItem(LS.cache) || 'null'); if (c) { st.day = c; st.date = c.date; st.today = c.today; } } catch {} }
      st.err = e.message;
    }
    render();
  }
  function allThings(day) { const out = []; for (const it of day.items) { out.push(it); for (const s of it.steps || []) out.push({ ...s, parent: it }); } return out; }
  function find(day, id) { for (const it of day.items) { if (it.id === id) return { x: it, parent: null }; for (const s of it.steps || []) if (s.id === id) return { x: s, parent: it }; } return null; }
  function isDone(x, parent) {
    const d = (st.day && st.day.done) || {};
    if (d[x.id]) return true;
    if (parent && d[parent.id]) return true;
    if (x.steps && x.steps.length) return x.steps.every(s => d[s.id]);
    return false;
  }
  /** NOW = the first not-done step of the block in progress (or the block itself). NEXT = what follows. */
  function nowNext(day) {
    const t = nowMs();
    const items = day.items.filter(i => !i.allDay);
    const inProgress = items.filter(i => i.startAt <= t && i.endAt > t && !isDone(i, null));
    let cur = inProgress.length ? inProgress[inProgress.length - 1] : null;
    let now = null;
    if (cur) now = (cur.steps && cur.steps.length) ? (cur.steps.find(s => !isDone(s, cur)) || null) : cur;
    if (now && now !== cur) now = { ...now, parent: cur };
    // linear order of things that can be done
    const linear = [];
    for (const it of items) { if (it.steps && it.steps.length) for (const s of it.steps) linear.push({ ...s, parent: it }); else linear.push(it); }
    let next = null;
    if (now) { const i = linear.findIndex(x => x.id === now.id && (x.parent ? x.parent.id === (now.parent && now.parent.id) : true)); next = linear.slice(i + 1).find(x => !isDone(x, x.parent)) || null; }
    else next = linear.find(x => x.startAt > t && !isDone(x, x.parent)) || linear.find(x => (x.parent ? x.parent.endAt : x.endAt) > t && !isDone(x, x.parent)) || null;
    return { now, next, cur };
  }
  async function toggleDone(id, value) {
    if (!st.day) return;
    const done = st.day.done || (st.day.done = {});
    if (value) done[id] = Date.now(); else delete done[id];
    render();
    try { const r = await api('/done', 'POST', { date: st.date, id, done: !!value }); st.day.done = r.done; }
    catch (e) { st.err = e.message; }
    render();
  }

  // ---------------------------------------------------------------- views
  function chip(goal, extra = '') { return `<span class="chip ${esc(goal)}">${esc(goal)}</span>${extra}`; }
  function topBar(title) {
    const dot = st.err ? 'bad' : (st.day && st.day.fromCache ? 'warn' : 'ok');
    const isToday = st.date === st.today;
    return `<div class="top">
      <div class="date">${title ? `<b>${esc(title)}</b>` : (isToday ? '<b>Today</b> · ' : '') + esc(st.date ? fmtDate(st.date) : '')}</div>
      <div class="right"><span class="dot ${dot}" title="${esc(st.err || 'live')}"></span>
        ${title ? '' : `<button class="iconbtn" data-action="prev" aria-label="Previous day">‹</button><button class="iconbtn" data-action="next" aria-label="Next day">›</button>`}
        <a class="iconbtn" href="#/settings" aria-label="Settings">⚙︎</a></div>
    </div>`;
  }
  function itemHref(x) { return `#/item/${encodeURIComponent(x.id)}?d=${st.date}`; }
  function nowCard(n) {
    const t = nowMs();
    const { now, next } = n;
    if (!now) {
      const nx = next;
      return `<section class="card now"><div class="eyebrow"><span class="k">Now</span><span class="time">${esc(new Date(t).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }))}</span></div>
        <h1>${nx ? 'Nothing running' : 'Day done'}</h1>
        <p class="how">${nx ? `Next: <b>${esc(nx.title)}</b> at ${esc(nx.startShort)}.` : 'Nothing left on the list.'}</p>
        ${nx ? `<div class="actions"><a class="btn" href="${itemHref(nx)}">Open next</a></div>` : ''}</section>`;
    }
    const late = t > now.startAt + 60000 && !now.parent;
    return `<section class="card now">
      <div class="eyebrow"><span class="k">Now</span><span class="time">${esc(now.startShort)}${now.endShort && !now.parent ? '–' + esc(now.endShort) : ''}</span>${chip(now.goal)}${now.draft ? '<span class="draft">draft</span>' : ''}</div>
      <h1>${esc(now.title)}</h1>
      ${now.parent ? `<p class="hint" style="margin:-6px 0 10px">in ${esc(now.parent.title)}</p>` : ''}
      ${now.how ? `<p class="how">${esc(now.how)}</p>` : ''}
      ${now.why ? `<p class="why"><b>Why · ${esc(GOAL_LABEL[now.goal] || now.goal)}</b>${esc(now.why)}</p>` : ''}
      <div class="actions"><button class="btn ok" data-action="done" data-id="${esc(now.id)}">✓ Done</button><a class="btn" href="${itemHref(now)}">Open</a></div>
    </section>
    ${next ? `<section class="card next"><span class="k">Next</span><span class="t">${esc(next.title)}</span><span class="time">${esc(next.startShort)}</span></section>` : ''}`;
  }
  function timeline(day, n) {
    const t = nowMs();
    const rows = day.items.map(it => {
      const done = isDone(it, null);
      const current = n.cur && n.cur.id === it.id;
      const past = it.endAt <= t && !current;
      const open = current || st.expanded[it.id];
      const steps = (it.steps || []);
      return `<div class="row ${current ? 'current' : ''} ${past ? 'past' : ''} ${done ? 'done' : ''}">
        <div class="time">${it.allDay ? 'all day' : esc(it.startShort)}<small>${it.allDay || it.auto || it.kind === 'alarm' ? '' : esc(it.endShort)}</small></div>
        <a class="title" href="${itemHref(it)}"><span class="gdot ${esc(it.goal)}"></span><span class="t">${esc(it.title)}</span>${it.notify ? '<span class="bell">🔔</span>' : ''}${steps.length ? `<button class="bell" data-action="expand" data-id="${esc(it.id)}">${open ? '▾' : '▸'} ${steps.length}</button>` : ''}</a>
        <button class="check ${done ? 'on' : ''}" data-action="toggle" data-id="${esc(it.id)}" data-on="${done ? 1 : 0}" aria-label="Done">✓</button>
      </div>
      ${steps.length && open ? `<div class="steps">${steps.map(s => {
        const sd = isDone(s, it); const isNow = n.now && n.now.id === s.id && n.now.parent && n.now.parent.id === it.id;
        return `<div class="row ${sd ? 'done' : ''} ${isNow ? 'current' : ''}"><div class="time">${esc(s.startShort)}</div>
          <a class="title" href="${itemHref(s)}"><span class="gdot ${esc(s.goal)}"></span><span class="t">${esc(s.title)}</span></a>
          <button class="check ${sd ? 'on' : ''}" data-action="toggle" data-id="${esc(s.id)}" data-on="${sd ? 1 : 0}" aria-label="Done">✓</button></div>`; }).join('')}</div>` : ''}`;
    }).join('');
    return `<section class="tl"><h2>${esc(day.dayName)} · ${day.items.length} blocks</h2>${rows || '<p class="empty">Nothing on the grid for this day.</p>'}</section>`;
  }
  function homeView() {
    if (!st.key) return keyView();
    if (!st.day) return topBar() + `<p class="empty">${st.err ? 'Could not load the day: ' + esc(st.err) : 'Loading…'}</p>`;
    const n = nowNext(st.day);
    const isToday = st.date === st.today;
    return topBar() + (isToday ? nowCard(n) : '') + timeline(st.day, isToday ? n : { cur: null, now: null }) + (st.err ? `<p class="err-msg">${esc(st.err)}</p>` : '') + `<p class="hint" style="text-align:center;margin-top:20px">rev ${esc(st.day.rev || '')} · v${VERSION}</p>`;
  }
  function keyView() {
    return `<div class="top"><div class="date"><b>Intention</b></div></div>
      <section class="card"><h2 style="font-size:22px">Pair this phone</h2>
      <p class="hint">Type the 6-character code Claude gave you. One use, then it is gone.</p>
      <label>Code</label><input id="codeIn" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" inputmode="text" maxlength="8" style="font-size:28px;letter-spacing:.25em;text-align:center;text-transform:uppercase">
      <div class="actions"><button class="btn primary" data-action="claim">Pair</button></div>
      <p class="${st.err && st.err !== 'unauthorized' ? 'err-msg' : 'hint'}" id="pairMsg">${esc(st.msg || '')}</p>
      <details style="margin-top:16px"><summary class="muted">Or paste the key</summary>
      <label>Key</label><input id="keyIn" type="password" autocomplete="off" autocapitalize="off" spellcheck="false">
      <div class="actions"><button class="btn ghost" data-action="savekey">Save key</button></div></details></section>`;
  }
  async function claimCode() {
    const code = (document.getElementById('codeIn').value || '').trim().toUpperCase();
    if (code.length < 6) { st.msg = 'Six characters.'; return render(); }
    try {
      const r = await fetch('/api/pair/claim', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ code }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.key) { st.err = j.error || 'HTTP ' + r.status; st.msg = 'That code did not work: ' + st.err + '. Ask Claude for a new one.'; return render(); }
      st.key = j.key; localStorage.setItem(LS.key, st.key); st.err = ''; st.msg = ''; render(); await loadDay();
    } catch (e) { st.msg = 'Network problem: ' + e.message; render(); }
  }
  function pageHtml(p) {
    if (!p) return '';
    if (p.type === 'table') return `<div class="page"><div class="hd">${esc(p.catName || '')} · ${esc(p.title)}</div><table class="tbl"><tr>${(p.columns || []).map(c => `<th>${esc(c)}</th>`).join('')}</tr>${(p.rows || []).filter(r => r.some(c => String(c).trim())).map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table></div>`;
    return `<div class="page"><div class="hd">${esc(p.catName || '')} · ${esc(p.title)}${p.half ? ` · half ${esc(p.half.toUpperCase())}` : ''}</div><pre>${esc(p.body || '')}</pre></div>`;
  }
  /** The in-depth sheet: steps with the intention under each, then how it helps as a whole. */
  function guideHtml(g) {
    if (!g || !Array.isArray(g.steps) || !g.steps.length) return '';
    return `<div class="section guide">
      ${g.title ? `<h3>${esc(g.title)}</h3>` : '<h3>Step by step</h3>'}
      ${g.intro ? `<p class="how">${esc(g.intro)}</p>` : ''}
      <ol class="gsteps">${g.steps.map(s => `<li><b>${esc(s.title || '')}</b>${s.do ? `<p>${esc(s.do)}</p>` : ''}${s.intention ? `<p class="gint"><span>Intention</span>${esc(s.intention)}</p>` : ''}</li>`).join('')}</ol>
      ${g.whole ? `<h3>How this helps as a whole</h3><p class="why" style="border-color:var(--now)">${esc(g.whole)}</p>` : ''}
      ${g.remember ? `<p class="hint" style="margin-top:12px"><b>Remember:</b> ${esc(g.remember)}</p>` : ''}
      ${g.source ? `<p class="hint">${esc(g.source)}</p>` : ''}
    </div>`;
  }
  function itemView(r) {
    if (!st.key) return keyView();
    if (!st.day || (r.date && r.date !== st.date)) return topBar('Item') + '<p class="empty">Loading…</p>';
    const f = find(st.day, r.id);
    if (!f) return topBar('Item') + `<p class="empty">That item is not on ${esc(fmtDate(st.date))}.</p><a class="btn ghost" href="#/">Back to today</a>`;
    const { x, parent } = f;
    const done = isDone(x, parent);
    const tool = toolHtml(x, parent);
    return `<a class="back" href="#/">‹ ${esc(parent ? parent.title : 'Today')}</a>
      <section class="item">
        <div class="meta"><span class="time">${esc(x.startShort)}${!parent ? '–' + esc(x.endShort) : ''}</span>${chip(x.goal)}${x.notify ? `<span class="chip plain">🔔 ${x.lead ? x.lead + ' min before' : 'on time'}</span>` : '<span class="chip plain">no ping</span>'}${x.draft ? '<span class="draft">draft</span>' : ''}${x.edited ? '<span class="draft">edited</span>' : ''}</div>
        <h1>${esc(x.title)}</h1>
        ${x.why ? `<p class="why"><b>Why · ${esc(GOAL_LABEL[x.goal] || x.goal)}</b>${esc(x.why)}</p>` : '<p class="why"><b>Why</b><span class="muted">Not written yet.</span></p>'}
        ${x.how ? `<div class="section"><h3>How</h3><p class="how">${esc(x.how)}</p></div>` : ''}
        ${guideHtml(x.guide)}
        ${x.checklist ? `<div class="section"><h3>Checklist</h3><ul class="cl" data-cl="${esc(x.id)}">${x.checklist.map((c, i) => `<li data-i="${i}"><span class="box">✓</span><span>${esc(c)}</span></li>`).join('')}</ul></div>` : ''}
        ${tool ? `<div class="section"><h3>Tool</h3>${tool}</div>` : ''}
        ${x.page ? `<div class="section"><h3>From the training app</h3>${pageHtml(x.page)}</div>` : ''}
        ${x.steps && x.steps.length ? `<div class="section"><h3>Steps</h3>${x.steps.map(s => `<div class="row ${isDone(s, x) ? 'done' : ''}"><div class="time">${esc(s.startShort)}</div><a class="title" href="${itemHref(s)}"><span class="gdot ${esc(s.goal)}"></span><span class="t">${esc(s.title)}</span></a><button class="check ${isDone(s, x) ? 'on' : ''}" data-action="toggle" data-id="${esc(s.id)}" data-on="${isDone(s, x) ? 1 : 0}">✓</button></div>`).join('')}</div>` : ''}
        ${x.source ? `<p class="hint">Source: ${esc(x.source)}</p>` : ''}
        <div class="sticky"><button class="btn ${done ? 'ghost' : 'ok'}" data-action="toggle" data-id="${esc(x.id)}" data-on="${done ? 1 : 0}">${done ? 'Done ✓ (tap to undo)' : '✓ Done'}</button></div>
        <div class="section"><button class="btn ghost small" data-action="edit">${st.editing ? 'Close editor' : 'Edit this item'}</button></div>
        ${st.editing ? editForm(x, parent) : ''}
      </section>`;
  }
  function editForm(x, parent) {
    return `<section class="card" id="editForm">
      <label>How</label><textarea id="e_how">${esc(x.how)}</textarea>
      <label>Why</label><textarea id="e_why">${esc(x.why)}</textarea>
      <label>Goal</label><select id="e_goal">${GOALS.map(g => `<option value="${g}" ${g === x.goal ? 'selected' : ''}>${GOAL_LABEL[g]}</option>`).join('')}</select>
      <div class="toggle" style="margin-top:14px"><span>Notify</span><button class="sw ${x.notify ? 'on' : ''}" id="e_notify" data-on="${x.notify ? 1 : 0}" aria-label="Notify"></button></div>
      <div class="grid2"><div><label>Lead (min before)</label><input id="e_lead" type="number" inputmode="numeric" value="${x.lead || 0}"></div>
      ${parent ? `<div><label>Duration (min)</label><input id="e_dur" type="number" inputmode="numeric" value="${x.duration || ''}"></div>` : '<div></div>'}</div>
      <div class="actions"><button class="btn primary" data-action="save">Save</button><button class="btn ghost small" data-action="reset">Reset to default</button></div>
      <p class="hint">${st.msg ? esc(st.msg) : 'Saved edits win over the defaults. Reset removes your edits for this item.'}</p>
    </section>`;
  }
  async function saveEdit(id) {
    const body = { how: document.getElementById('e_how').value, why: document.getElementById('e_why').value, goal: document.getElementById('e_goal').value, notify: document.getElementById('e_notify').dataset.on === '1', lead: +document.getElementById('e_lead').value || 0 };
    const dur = document.getElementById('e_dur'); if (dur && dur.value !== '') body.duration = +dur.value;
    try { await api('/item/' + encodeURIComponent(id), 'PUT', body); st.msg = 'Saved.'; st.editing = false; await loadDay(); }
    catch (e) { st.msg = 'Save failed: ' + e.message; render(); }
  }

  // ---------------------------------------------------------------- tools
  function toolHtml(x) {
    const tl = x.tool || 'none';
    const id = esc(x.id);
    if (tl === 'timer') {
      const mins = x.duration || 5;
      return `<div class="tool timer" data-tool="timer" data-min="${mins}"><div class="big" id="t_big">${String(mins).padStart(2, '0')}:00</div>
        <div class="actions"><button class="btn primary" data-action="t_start">Start ${mins} min</button><button class="btn ghost small" data-action="t_reset">Reset</button></div>
        <p class="rs" style="margin-top:14px"><b>The 6 R's</b> when you catch the mind wandering:<br>recognize · release · relax · re-smile · rebreathe · return</p></div>`;
    }
    if (tl === 'breath-478' || tl === 'breath-down' || tl === 'breath') {
      return `<div class="tool breath" data-tool="breath"><div class="seg"><button data-action="b_mode" data-mode="478" class="on">4-7-8</button><button data-action="b_mode" data-mode="46">4 in · 6 out</button><button data-action="b_mode" data-mode="down">DOWN</button></div>
        <div class="orb" id="b_orb"></div><div class="phase" id="b_phase">Ready</div><div class="count" id="b_count">Lying down. Longer out than in.</div>
        <div class="actions"><button class="btn primary" data-action="b_start">Start</button><button class="btn ghost small" data-action="b_stop">Stop</button></div>
        <p class="hint">DOWN = two inhales through the nose (a full one, then a short top-up), one long slow exhale out the mouth.</p></div>`;
    }
    if (tl === 'coherence') {
      return `<div class="tool"><p><b>Open the Coherence app</b> and run Vortex. Come back and tap Done.</p>
        <p class="safety">Holds lying down, or sitting on the floor or bed. End every hold at the first real urge. Never standing, walking, in water or driving.</p></div>`;
    }
    if (tl === 'journal-am') {
      const j = (st.tools.journal && st.tools.journal.am) || {};
      return `<div class="tool journal" data-tool="journal" data-part="am">
        <label>Today I hunt — one thing</label><textarea id="j_hunt" placeholder="Practice days: off Practice Intentions.">${esc(j.hunt || '')}</textarea>
        <label>Being seen — today's rep</label><textarea id="j_seen" placeholder="Off your list on PRESSURE.">${esc(j.seen || '')}</textarea>
        <label>I am — three lines, out loud, chest up</label><textarea id="j_iam" placeholder="Then your ethos, once.">${esc(j.iam || '')}</textarea>
        <div class="actions"><button class="btn primary" data-action="j_save">Save</button></div><p class="hint" id="j_msg">${esc(st.msg || 'Private. Saved to Intention only.')}</p></div>`;
    }
    if (tl === 'journal-pm') {
      const j = (st.tools.journal && st.tools.journal.pm) || {};
      return `<div class="tool journal" data-tool="journal" data-part="pm">
        <label>Red — one line, then close it</label><textarea id="j_red" placeholder="A moment you played small, and what you do instead next time. Game nights: skip it.">${esc(j.red || '')}</textarea>
        <label>Blue — three wins (one from a moment you were nervous or watched)</label>
        <input id="j_b1" placeholder="1" value="${esc(j.b1 || '')}" style="margin-bottom:8px"><input id="j_b2" placeholder="2" value="${esc(j.b2 || '')}" style="margin-bottom:8px"><input id="j_b3" placeholder="3" value="${esc(j.b3 || '')}">
        <label>Next — the energy you bring into tomorrow</label><textarea id="j_next">${esc(j.next || '')}</textarea>
        <label>Best win → BLUE LIST</label><div class="grid2"><input id="j_best" placeholder="The proof" value="${esc(j.best || '')}"><input id="j_line" placeholder="What was on the line" value="${esc(j.line || '')}"></div>
        <div class="actions"><button class="btn primary" data-action="j_save">Save</button><button class="btn ghost small" data-action="j_blue">Add to BLUE LIST</button></div><p class="hint" id="j_msg">${esc(st.msg || 'Private. The BLUE LIST row goes to your training app.')}</p></div>`;
    }
    if (tl === 'belief') return `<div class="tool"><p style="font-size:20px;font-weight:700">${esc(x.how)}</p><p class="hint">Answer it, then Done.</p></div>`;
    if (tl === 'sixr') return `<div class="tool"><ul class="cl" data-cl="sixr">${['Recognize', 'Release', 'Relax', 'Re-smile', 'Rebreathe', 'Return'].map((w, i) => `<li data-i="${i}"><span class="box">✓</span><span style="font-size:22px;font-weight:700">${w}</span></li>`).join('')}</ul><p class="hint">Tap each as you do it, then Done.</p></div>`;
    if (tl === 'fifty' || tl === 'pressure-test' || tl === 'bolt' || tl === 'visualization') return `<div class="tool"><p class="muted">This tool arrives in the next build. For now: do it the way the page says, then Done.</p></div>`;
    return '';
  }
  // timer
  let timerH = null, timerEnd = 0;
  function tick() { const el = document.getElementById('t_big'); if (!el) { clearInterval(timerH); timerH = null; return; } const left = Math.max(0, timerEnd - Date.now()); const s = Math.round(left / 1000); el.textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; if (left <= 0) { clearInterval(timerH); timerH = null; el.textContent = 'Done'; try { navigator.vibrate && navigator.vibrate([200, 100, 200]); } catch {} } }
  // breath
  let breathH = null, breathMode = '478';
  const PATTERNS = { '478': [['Breathe in', 4], ['Hold', 7], ['Breathe out', 8]], '46': [['Breathe in', 4], ['Breathe out', 6]], 'down': [['In (nose)', 2], ['Top-up (nose)', 1], ['Out, long and slow', 7]] };
  function breathStart() {
    breathStop(); const pat = PATTERNS[breathMode]; let i = 0, left = 0;
    const orb = document.getElementById('b_orb'), ph = document.getElementById('b_phase'), ct = document.getElementById('b_count');
    const step = () => { if (!document.getElementById('b_orb')) return breathStop(); if (left <= 0) { const [name, secs] = pat[i % pat.length]; i++; left = secs; ph.textContent = name; const grow = /in|top/i.test(name) ? 1 : (/hold/i.test(name) ? null : 0.6); if (grow !== null) { orb.style.transition = `transform ${secs}s linear`; orb.style.transform = `scale(${grow})`; } } ct.textContent = left + 's'; left--; };
    step(); breathH = setInterval(step, 1000);
  }
  function breathStop() { if (breathH) clearInterval(breathH); breathH = null; }
  async function journalLoad() { if (!st.date) return; try { st.tools.journal = await api('/journal?date=' + st.date); } catch {} }
  async function journalSave(part) {
    const g = (id) => (document.getElementById(id) || {}).value || '';
    const data = part === 'am' ? { hunt: g('j_hunt'), seen: g('j_seen'), iam: g('j_iam') } : { red: g('j_red'), b1: g('j_b1'), b2: g('j_b2'), b3: g('j_b3'), next: g('j_next'), best: g('j_best'), line: g('j_line') };
    try { const r = await api('/journal', 'POST', { date: st.date, part, data }); st.tools.journal = r.journal; st.msg = 'Saved ' + new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }); }
    catch (e) { st.msg = 'Save failed: ' + e.message; }
    render();
  }
  async function blueList() {
    const g = (id) => (document.getElementById(id) || {}).value || '';
    const proof = g('j_best') || g('j_b1'); const line = g('j_line');
    if (!proof.trim()) { st.msg = 'Write the win first.'; return render(); }
    await journalSave('pm');
    const [y, m, d] = st.date.split('-'); const label = `${new Date(Date.UTC(+y, +m - 1, +d)).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })}`;
    try { const r = await api('/writeback', 'POST', { table: 'blue', row: [label, proof, line] }); st.msg = `On the BLUE LIST (row ${r.changed[0].index + 1}).`; }
    catch (e) { st.msg = 'BLUE LIST write failed: ' + e.message; }
    render();
  }

  // ---------------------------------------------------------------- settings
  async function notifStatus() {
    const s = { standalone: !!(navigator.standalone || (window.matchMedia && matchMedia('(display-mode: standalone)').matches)), supported: 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window, permission: (window.Notification && Notification.permission) || 'n/a', subscribed: false };
    try { if (s.supported) { const reg = await navigator.serviceWorker.getRegistration(); const sub = reg && await reg.pushManager.getSubscription(); s.subscribed = !!sub; s.endpointHost = sub ? new URL(sub.endpoint).host : ''; } } catch {}
    st.notif = s; return s;
  }
  function settingsView() {
    if (!st.key) return keyView();
    const n = st.notif || {}; const s = st.settings || {};
    const daySel = (name, val) => `<select data-set="${name}"><option value="">—</option><option value="*" ${val === '*' ? 'selected' : ''}>Daily</option>${DAYS.map((d, i) => `<option value="${i}" ${String(val) === String(i) ? 'selected' : ''}>${d}</option>`).join('')}</select>`;
    const timeIn = (name, val) => `<input type="time" data-set="${name}" value="${esc(val || '')}">`;
    const hpv = s.hpv || [{}, {}, {}], al = s.alarms || [{}, {}, {}];
    return topBar('Settings') + `
      <section class="card"><h2 style="font-size:20px">Notifications</h2>
        <div class="kv"><span>Installed to Home Screen</span><span>${n.standalone ? 'yes' : 'no — Share → Add to Home Screen'}</span></div>
        <div class="kv"><span>Push supported here</span><span>${n.supported ? 'yes' : 'no'}</span></div>
        <div class="kv"><span>Permission</span><span>${esc(n.permission)}</span></div>
        <div class="kv"><span>Subscribed</span><span>${n.subscribed ? 'yes' : 'no'}</span></div>
        <div class="actions"><button class="btn primary" data-action="enable">Enable notifications</button></div>
        <div class="actions"><button class="btn ghost" data-action="test1">Test: ping me in 1 min</button><button class="btn ghost small" data-action="test0">Now</button></div>
        <p class="${st.msgKind === 'err' ? 'err-msg' : st.msgKind === 'ok' ? 'ok-msg' : 'hint'}" id="n_msg" style="font-size:16px">${esc(st.msg || (n.standalone ? '' : 'Open Intention from its Home Screen icon, not Safari, for notifications to work.'))}</p></section>
      <section class="card"><h2 style="font-size:20px">Weekly items</h2><p class="hint">Unplaced = not shown. Times are yours.</p>
        <label>Pressure test</label><div class="grid2">${daySel('pressureTest.day', s.pressureTest && s.pressureTest.day)}${timeIn('pressureTest.time', s.pressureTest && s.pressureTest.time)}</div>
        <label>High pressure visualization — 3 mornings</label>${[0, 1, 2].map(i => `<div class="grid2" style="margin-bottom:8px">${daySel('hpv.' + i + '.day', hpv[i] && hpv[i].day)}${timeIn('hpv.' + i + '.time', hpv[i] && hpv[i].time)}</div>`).join('')}
        <label>Belief alarms — 3 a day</label>${[0, 1, 2].map(i => `<div class="grid2" style="margin-bottom:8px">${daySel('alarms.' + i + '.day', (al[i] && al[i].day) ?? '*')}${timeIn('alarms.' + i + '.time', al[i] && al[i].time)}</div>`).join('')}
        <label>BOLT — once a week, before Vortex</label><div class="grid2">${daySel('bolt.day', s.bolt && s.bolt.day)}${timeIn('bolt.time', s.bolt && s.bolt.time)}</div>
        <label>6 R's a day</label><input type="number" inputmode="numeric" min="0" max="12" data-set="sixr.count" value="${esc(s.sixr && s.sixr.count !== undefined && s.sixr.count !== '' ? s.sixr.count : 4)}">
        <p class="hint">Random times, new every day. Never in class or practice, at least an hour apart. 0 turns them off.</p>
        <div class="actions"><button class="btn primary" data-action="savesettings">Save weekly items</button></div></section>
      <section class="card"><h2 style="font-size:20px">Key</h2><p class="hint">Stored on this phone only.</p><div class="actions"><button class="btn ghost danger small" data-action="forgetkey">Forget key</button></div></section>
      <p class="hint" style="text-align:center">Intention v${VERSION}</p>`;
  }
  function b64ToU8(b64) { const p = '='.repeat((4 - b64.length % 4) % 4); const s = (b64 + p).replace(/-/g, '+').replace(/_/g, '/'); const raw = atob(s); return Uint8Array.from([...raw].map(c => c.charCodeAt(0))); }
  const isStandalone = () => !!(navigator.standalone || (window.matchMedia && matchMedia('(display-mode: standalone)').matches));
  // Each step of enabling notifications is reported to the server so it can be debugged without seeing the phone.
  function diag(step, detail) {
    const ev = { t: Date.now(), step, detail: detail == null ? '' : String(detail).slice(0, 300) };
    (st.diagQ = st.diagQ || []).push(ev);
    clearTimeout(st.diagT);
    st.diagT = setTimeout(() => { const q = st.diagQ || []; st.diagQ = []; if (st.key && q.length) api('/diag', 'POST', { events: q }).catch(() => {}); }, 400);
  }
  async function swReg() {
    if (!('serviceWorker' in navigator)) throw new Error('no service worker support');
    let reg = await navigator.serviceWorker.getRegistration('/');
    if (!reg) reg = await navigator.serviceWorker.register('/sw.js');
    const timeout = new Promise((_, rej) => setTimeout(() => rej(new Error('service worker not ready after 8 s')), 8000));
    return Promise.race([navigator.serviceWorker.ready, timeout]);
  }
  async function subscribeAndSave(reg, why) {
    const { vapidPublicKey } = await api('/config');
    if (!vapidPublicKey) throw new Error('server has no VAPID public key');
    let sub = await reg.pushManager.getSubscription();
    if (!sub) { sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(vapidPublicKey) }); diag('subscribed', new URL(sub.endpoint).host); }
    const r = await api('/subscribe', 'POST', { subscription: sub.toJSON(), ua: navigator.userAgent, why });
    localStorage.setItem(LS.subscribed, '1');
    diag('saved-on-server', 'devices ' + r.count);
    return r;
  }
  async function enablePush() {
    st.msg = ''; st.msgKind = '';
    diag('enable-tap', `standalone=${isStandalone()} perm=${window.Notification ? Notification.permission : 'n/a'} push=${'PushManager' in window}`);
    if (!('Notification' in window) || !('PushManager' in window)) {
      st.msg = isStandalone() ? 'This phone cannot do web push here (needs iOS 16.4 or newer).' : 'Push only works from the Home Screen app. In Safari: Share → Add to Home Screen. Then open Intention from that icon and tap Enable there.';
      st.msgKind = 'err'; diag('enable-fail', 'no push api'); return render();
    }
    let perm = Notification.permission;
    try { if (perm !== 'granted') perm = await Notification.requestPermission(); } catch (e) { diag('perm-error', e && e.message); }
    diag('permission', perm);
    if (perm !== 'granted') {
      st.msg = perm === 'denied' ? 'Notifications are blocked for Intention. iPhone Settings → Notifications → Intention → Allow Notifications, then tap Enable again.' : 'No permission yet. Tap Enable and choose Allow.';
      st.msgKind = 'err'; await notifStatus(); return render();
    }
    try {
      const reg = await swReg(); diag('sw-ready', reg.scope);
      const r = await subscribeAndSave(reg, 'enable');
      st.msg = `Subscribed. The server knows ${r.count} device${r.count === 1 ? '' : 's'}. Now tap a test.`; st.msgKind = 'ok';
    } catch (e) { st.msg = 'Subscribe failed: ' + (e && (e.name + ': ' + e.message) || e) + '. Tell Claude this line.'; st.msgKind = 'err'; diag('enable-fail', e && (e.name + ': ' + e.message)); }
    await notifStatus(); render();
  }
  /** Self-heal: if permission is already granted, make sure the server has this phone's subscription. */
  async function autoSubscribe(reason) {
    try {
      if (!st.key || !('PushManager' in window) || !window.Notification || Notification.permission !== 'granted') return;
      const reg = await swReg();
      await subscribeAndSave(reg, reason);
    } catch (e) { diag('auto-subscribe-fail', e && (e.name + ': ' + e.message)); }
  }
  async function testPush(inMinutes) {
    diag('test-tap', 'inMinutes ' + inMinutes);
    try { const r = await api('/push-test', 'POST', { inMinutes }); st.msgKind = 'ok'; st.msg = inMinutes ? `Scheduled. Lands about ${new Date(r.landsAt).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}. Lock the phone and wait.` : `Sent now to ${r.sent} device(s)${r.errors && r.errors.length ? ': ' + r.errors.join('; ') : ''}.`; }
    catch (e) { st.msgKind = 'err'; st.msg = 'Test failed: ' + e.message; }
    render();
  }
  async function saveSettings() {
    const out = { ...(st.settings || {}) };
    document.querySelectorAll('[data-set]').forEach(el => { const path = el.dataset.set.split('.'); let o = out; for (let i = 0; i < path.length - 1; i++) { const k = path[i]; const nextIsIdx = /^\d+$/.test(path[i + 1]); if (o[k] == null) o[k] = nextIsIdx ? [] : {}; o = o[k]; } o[path[path.length - 1]] = el.value; });
    try { const r = await api('/settings', 'PUT', out); st.settings = r.settings; st.msg = 'Weekly items saved.'; await loadDay(); }
    catch (e) { st.msg = 'Save failed: ' + e.message; }
    render();
  }

  // ---------------------------------------------------------------- render + events
  function render() {
    const r = route();
    let html = '';
    if (r.view === 'settings') html = settingsView();
    else if (r.view === 'item') html = itemView(r);
    else html = homeView();
    $app.innerHTML = html;
    if (r.view === 'item') { const el = document.querySelector('[data-tool="timer"]'); if (el && timerH) tick(); }
  }
  document.addEventListener('click', async (e) => {
    const a = e.target.closest('[data-action]');
    if (a) {
      const act = a.dataset.action, id = a.dataset.id;
      if (act === 'toggle') { e.preventDefault(); return toggleDone(id, a.dataset.on !== '1'); }
      if (act === 'done') { e.preventDefault(); return toggleDone(id, true); }
      if (act === 'expand') { e.preventDefault(); st.expanded[id] = !st.expanded[id]; return render(); }
      if (act === 'prev') return loadDay(st.day ? st.day.prev : null);
      if (act === 'next') return loadDay(st.day ? st.day.next : null);
      if (act === 'claim') return claimCode();
      if (act === 'savekey') { const v = (document.getElementById('keyIn').value || '').trim(); if (!v) return; st.key = v; localStorage.setItem(LS.key, v); st.err = ''; render(); return loadDay(); }
      if (act === 'forgetkey') { if (!confirm('Forget the key on this phone?')) return; localStorage.removeItem(LS.key); st.key = ''; st.day = null; return render(); }
      if (act === 'edit') { st.editing = !st.editing; st.msg = ''; return render(); }
      if (act === 'save') { const r = route(); return saveEdit(r.id); }
      if (act === 'reset') { const r = route(); try { await api('/item/' + encodeURIComponent(r.id), 'DELETE'); st.msg = 'Reset.'; st.editing = false; await loadDay(); } catch (err) { st.msg = err.message; render(); } return; }
      if (act === 'enable') return enablePush();
      if (act === 'test1') return testPush(1);
      if (act === 'test0') return testPush(0);
      if (act === 'savesettings') return saveSettings();
      if (act === 't_start') { const box = a.closest('[data-tool]'); timerEnd = Date.now() + (+box.dataset.min || 5) * 60000; if (timerH) clearInterval(timerH); timerH = setInterval(tick, 250); tick(); return; }
      if (act === 't_reset') { if (timerH) clearInterval(timerH); timerH = null; const box = a.closest('[data-tool]'); document.getElementById('t_big').textContent = `${String(+box.dataset.min || 5).padStart(2, '0')}:00`; return; }
      if (act === 'b_mode') { breathMode = a.dataset.mode; a.parentElement.querySelectorAll('button').forEach(b => b.classList.toggle('on', b === a)); return; }
      if (act === 'b_start') return breathStart();
      if (act === 'b_stop') { breathStop(); const ph = document.getElementById('b_phase'); if (ph) ph.textContent = 'Stopped'; return; }
      if (act === 'j_save') { const box = a.closest('[data-tool]'); return journalSave(box.dataset.part); }
      if (act === 'j_blue') return blueList();
    }
    const li = e.target.closest('.cl li'); if (li) { li.classList.toggle('on'); }
    const sw = e.target.closest('.sw'); if (sw) { const on = sw.dataset.on === '1'; sw.dataset.on = on ? '0' : '1'; sw.classList.toggle('on', !on); }
  });
  window.addEventListener('hashchange', async () => {
    st.editing = false; st.msg = '';
    const km = /(?:^#|[#&?])key=([^&]+)/.exec(location.hash);
    if (km) { st.key = decodeURIComponent(km[1]); localStorage.setItem(LS.key, st.key); history.replaceState(null, '', location.pathname + '#/'); render(); return loadDay(); }
    const r = route();
    if (r.view === 'item' && r.date && r.date !== st.date) await loadDay(r.date);
    if (r.view === 'settings') { st.settings = st.settings || await api('/settings').catch(() => ({})); await notifStatus(); }
    if (r.view === 'item') { const f = st.day && find(st.day, r.id); if (f && /journal/.test(f.x.tool || '')) await journalLoad(); }
    render();
  });
  /** A tapped notification parks its target in cache storage (see sw.js). Read it whenever the app wakes up. */
  async function consumePending() {
    if (st.pendingBusy) return false;   // boot + pageshow + focus can all ask at once
    st.pendingBusy = true;
    try {
      if (!('caches' in window)) return false;
      const c = await caches.open('intention-nav');
      const r = await c.match('/__pending');
      if (!r) return false;
      const j = await r.json().catch(() => null);
      await c.delete('/__pending');
      if (!j || !j.url || Date.now() - j.t > 15 * 60000) return false;
      const h = new URL(j.url, location.origin).hash || '#/';
      diag('nav-from-notification', h);
      if (location.hash !== h) location.hash = h; else render();
      return true;
    } catch (e) { diag('pending-error', e && e.message); return false; }
    finally { st.pendingBusy = false; }
  }
  function goToUrl(url) { try { const h = new URL(url, location.origin).hash || '#/'; if (location.hash !== h) location.hash = h; else render(); } catch {} }
  /** Upload whatever the service worker logged (push received, click handling) as diagnostics. */
  async function flushSwLog() {
    try {
      if (!('caches' in window)) return;
      const c = await caches.open('intention-nav');
      const r = await c.match('/__swlog');
      if (!r) return;
      const arr = await r.json().catch(() => []);
      await c.delete('/__swlog');
      for (const e of arr) diag('sw:' + e.step, new Date(e.t).toISOString().slice(11, 19) + ' ' + e.detail);
    } catch {}
  }
  /** iOS can launch the app at its start page when a notification is tapped, ignoring the service
   *  worker. So: if the server pinged something in the last 10 minutes and we have not shown it
   *  yet on this phone, open that item. */
  function maybeRouteToLastPush() {
    const lp = st.day && st.day.lastPush;
    if (!lp || !lp.url || !lp.at) return false;
    let seen = 0; try { seen = +localStorage.getItem('intention.lastPushSeen') || 0; } catch {}
    if (lp.at <= seen || Date.now() - lp.at > 10 * 60000) return false;
    try { localStorage.setItem('intention.lastPushSeen', String(lp.at)); } catch {}
    const h = new URL(lp.url, location.origin).hash || '#/';
    if (location.hash === h) return false;
    diag('nav-from-last-push', h);
    location.hash = h;
    return true;
  }
  async function wokeUp() {
    await flushSwLog();
    const routed = await consumePending();
    await loadDay();
    if (!routed) maybeRouteToLastPush();
    try { const reg = await navigator.serviceWorker.getRegistration(); if (reg) reg.update(); } catch {}
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) wokeUp(); });
  window.addEventListener('focus', () => consumePending());
  window.addEventListener('pageshow', () => consumePending());
  setInterval(() => { if (!document.hidden && route().view === 'home') render(); }, 30000);
  setInterval(() => { if (!document.hidden) loadDay(); }, 120000);

  // ---------------------------------------------------------------- boot
  (async () => {
    // one-time key handoff: /#key=... (never hits the server)
    const km = /(?:^#|[#&?])key=([^&]+)/.exec(location.hash);
    if (km) { st.key = decodeURIComponent(km[1]); localStorage.setItem(LS.key, st.key); history.replaceState(null, '', location.pathname + '#/'); }
    let swOk = 'none';
    if ('serviceWorker' in navigator) {
      try { const reg = await navigator.serviceWorker.register('/sw.js'); swOk = reg.scope; } catch (e) { swOk = 'error: ' + (e && e.message); console.warn('sw', e); }
      navigator.serviceWorker.addEventListener('message', (e) => { if (e.data && e.data.type === 'open') { diag('nav-from-message', e.data.url); goToUrl(e.data.url); } });
    }
    const bootHash = location.hash;
    await flushSwLog();
    const routed = await consumePending();
    render();
    if (st.key) {
      diag('boot', `v${VERSION} standalone=${isStandalone()} sw=${swOk} push=${'PushManager' in window} perm=${window.Notification ? Notification.permission : 'n/a'} hash=${bootHash.slice(0, 60)}`);
      autoSubscribe('boot');
      const r = route();
      await loadDay(r.view === 'item' && r.date ? r.date : null);
      if (!routed && r.view !== 'item') maybeRouteToLastPush();
      if (r.view === 'settings') { st.settings = await api('/settings').catch(() => ({})); await notifStatus(); render(); }
      if (r.view === 'item') { const f = st.day && find(st.day, r.id); if (f && /journal/.test(f.x.tool || '')) { await journalLoad(); render(); } }
    }
  })();
})();
