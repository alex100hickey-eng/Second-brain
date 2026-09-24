// Intention API. Every route except /api/health needs `Authorization: Bearer <INTENTION_KEY>`.
// See README.md for the routes and how a Claude session updates items from chat.
import { randomBytes } from 'node:crypto';
import content from '../../content/items.json' with { type: 'json' };
import { decodeSnapshot, buildDay, nowParts, gridDateFor, addDays, TZ, GOALS } from '../../lib/day.mjs';
import { getJSON, setJSON, K } from './lib/store.mjs';
import { fetchSnapshot } from './lib/snapshot.mjs';
import { authorized } from './lib/auth.mjs';
import { subId, sendToAll } from './lib/push.mjs';
import { runTick, itemUrl, payloadFor, loadDays } from './lib/due.mjs';
import { writeBack } from './lib/writeback.mjs';

const PAIR_TTL_MS = 24 * 60 * 60 * 1000;
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
const bad = (msg, status = 400) => json({ error: msg }, status);
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const EDITABLE = ['goal', 'how', 'why', 'tool', 'notify', 'lead', 'duration', 'title', 'guide'];

async function readBody(req) { try { return await req.json(); } catch { return null; } }

export default async (req) => {
  const url = new URL(req.url);
  const path = url.pathname.replace(/^\/api/, '') || '/';
  const m = req.method;

  if (path === '/health') return json({ ok: true, now: new Date().toISOString(), tz: TZ });

  // ---- pairing: a short single-use code (made from an authorized session) hands the key to a new phone.
  // Home Screen web apps on iOS have their own storage, so a link cannot carry the key into the installed app.
  if (path === '/pair/claim' && m === 'POST') {
    const b = await readBody(req);
    const code = String((b && b.code) || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (code.length < 6) return bad('code required', 400);
    const pairs = await getJSON(K.pairs, {});
    const rec = pairs[code];
    const fresh = rec && Date.now() - rec.createdAt < PAIR_TTL_MS;
    for (const [c, r] of Object.entries(pairs)) if (Date.now() - r.createdAt >= PAIR_TTL_MS) delete pairs[c];
    if (rec) delete pairs[code];
    await setJSON(K.pairs, pairs);
    if (!fresh) return bad('code invalid or expired', 403);
    return json({ ok: true, key: process.env.INTENTION_KEY });
  }
  if (!authorized(req)) return bad('unauthorized', 401);
  if (path === '/pair' && m === 'POST') {
    const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
    let code = ''; const bytes = randomBytes(6); for (const x of bytes) code += alphabet[x % alphabet.length];
    const pairs = await getJSON(K.pairs, {});
    pairs[code] = { createdAt: Date.now() };
    await setJSON(K.pairs, pairs);
    return json({ ok: true, code, expiresAt: new Date(Date.now() + PAIR_TTL_MS).toISOString() });
  }

  const parts = nowParts();
  const today = gridDateFor(parts);

  // ---- the day
  if (path === '/day' && m === 'GET') {
    const date = url.searchParams.get('date') || today;
    if (!DATE_RE.test(date)) return bad('date must be YYYY-MM-DD');
    const [{ snap, fromCache }, overrides, settings, done, lastPush] = await Promise.all([
      fetchSnapshot(), getJSON(K.overrides, {}), getJSON(K.settings, {}), getJSON(K.done(date), {}), getJSON(K.lastPush, null),
    ]);
    const day = buildDay(decodeSnapshot(snap), date, content, overrides, settings);
    return json({ ...day, done, today, now: Date.now(), fromCache, lastPush, prev: addDays(date, -1), next: addDays(date, 1) });
  }
  if (path === '/last-push' && m === 'GET') return json(await getJSON(K.lastPush, null));

  // ---- done taps (the log)
  if (path === '/done' && m === 'POST') {
    const b = await readBody(req);
    if (!b || !DATE_RE.test(b.date || '') || !b.id) return bad('need {date, id, done}');
    const key = K.done(b.date);
    const done = await getJSON(key, {});
    if (b.done === false) delete done[b.id]; else done[b.id] = Date.now();
    await setJSON(key, done);
    return json({ ok: true, done });
  }

  // ---- per-item edits (overrides win over content/items.json)
  if (path === '/overrides' && m === 'GET') return json(await getJSON(K.overrides, {}));
  if (path.startsWith('/item/') && (m === 'PUT' || m === 'PATCH' || m === 'DELETE')) {
    const id = decodeURIComponent(path.slice('/item/'.length));
    if (!id) return bad('missing id');
    const all = await getJSON(K.overrides, {});
    if (m === 'DELETE') { delete all[id]; await setJSON(K.overrides, all); return json({ ok: true, id, override: null }); }
    const b = await readBody(req);
    if (!b || typeof b !== 'object') return bad('body must be an object of fields');
    const cur = all[id] || {};
    for (const f of EDITABLE) {
      if (b[f] === undefined) continue;
      if (b[f] === null) { delete cur[f]; continue; }
      if (f === 'goal' && !GOALS.includes(b[f])) return bad(`goal must be one of ${GOALS.join(', ')}`);
      if ((f === 'lead' || f === 'duration') && !(Number.isFinite(+b[f]) && +b[f] >= 0)) return bad(`${f} must be a number >= 0`);
      if (f === 'guide') {
        const g = b[f];
        const ok = typeof g === 'string' || (g && typeof g === 'object' && Array.isArray(g.steps) && g.steps.every(s => s && typeof s === 'object'));
        if (!ok) return bad('guide must be {title, intro, steps:[{title, do, intention}], whole, remember, source} or the name of a shared guide');
        cur[f] = g; continue;
      }
      cur[f] = (f === 'notify') ? !!b[f] : (f === 'lead' || f === 'duration') ? +b[f] : String(b[f]);
    }
    cur.updatedAt = Date.now();
    all[id] = cur;
    await setJSON(K.overrides, all);
    return json({ ok: true, id, override: cur });
  }

  // ---- settings (weekly placements + prefs)
  if (path === '/settings' && m === 'GET') return json(await getJSON(K.settings, {}));
  if (path === '/settings' && (m === 'PUT' || m === 'PATCH')) {
    const b = await readBody(req);
    if (!b || typeof b !== 'object') return bad('body must be an object');
    const cur = m === 'PATCH' ? { ...(await getJSON(K.settings, {})), ...b } : b;
    cur.updatedAt = Date.now();
    await setJSON(K.settings, cur);
    return json({ ok: true, settings: cur });
  }

  // ---- push subscriptions
  if (path === '/config' && m === 'GET') return json({ vapidPublicKey: process.env.VAPID_PUBLIC_KEY || null, today, now: Date.now() });
  if (path === '/subscribe' && m === 'POST') {
    const b = await readBody(req);
    const sub = b && b.subscription;
    if (!sub || !sub.endpoint) return bad('need {subscription}');
    const all = await getJSON(K.subs, {});
    const id = subId(sub);
    all[id] = { subscription: sub, ua: (b.ua || '').slice(0, 200), addedAt: all[id]?.addedAt || Date.now(), seenAt: Date.now() };
    await setJSON(K.subs, all);
    return json({ ok: true, id, count: Object.keys(all).length });
  }
  if (path === '/subscribe' && m === 'DELETE') {
    const b = await readBody(req);
    const all = await getJSON(K.subs, {});
    if (b && b.endpoint) { for (const [id, r] of Object.entries(all)) if (r.subscription.endpoint === b.endpoint) delete all[id]; }
    await setJSON(K.subs, all);
    return json({ ok: true, count: Object.keys(all).length });
  }
  if (path === '/subscriptions' && m === 'GET') {
    const all = await getJSON(K.subs, {});
    return json(Object.entries(all).map(([id, r]) => ({ id, ua: r.ua, addedAt: r.addedAt, seenAt: r.seenAt, endpointHost: new URL(r.subscription.endpoint).host, endpointTail: r.subscription.endpoint.slice(-8) })));
  }

  // ---- client diagnostics: the app reports each step of enabling notifications so it can be debugged from here
  if (path === '/diag' && m === 'POST') {
    const b = await readBody(req);
    const events = Array.isArray(b && b.events) ? b.events.slice(0, 50) : [];
    const cur = await getJSON(K.diag, []);
    const ua = (req.headers.get('user-agent') || '').slice(0, 160);
    for (const e of events) cur.unshift({ t: +e.t || Date.now(), step: String(e.step || '').slice(0, 40), detail: String(e.detail || '').slice(0, 300), ua });
    if (cur.length > 200) cur.length = 200;
    await setJSON(K.diag, cur);
    return json({ ok: true, count: cur.length });
  }
  if (path === '/diag' && m === 'GET') return json(await getJSON(K.diag, []));

  // ---- test push: lands in `inMinutes` (default 1) and opens the given/current item
  if (path === '/push-test' && m === 'POST') {
    const b = (await readBody(req)) || {};
    const inMinutes = Math.max(0, Math.min(60, +b.inMinutes || 0));
    if (!Object.keys(await getJSON(K.subs, {})).length) return bad('No phone is subscribed yet. Tap Enable notifications first (from the Home Screen app).', 409);
    let itemId = b.id, date = DATE_RE.test(b.date || '') ? b.date : today, title = 'Intention test', body = 'Tap me — I should open the right item.';
    if (!itemId) {
      const { days } = await loadDays(Date.now());
      const day = days[1];
      const now = Date.now();
      const cur = [...day.items].reverse().find(i => i.startAt <= now && i.endAt > now) || day.items.find(i => i.startAt > now) || day.items[0];
      if (cur) { const first = cur.steps && cur.steps[0]; const x = first || cur; itemId = x.id; title = x.title; body = (x.how || x.why || 'Tap to open').slice(0, 140); date = day.date; }
    }
    const payload = { title: b.title || title, body: b.body || body, url: itemUrl(itemId || 'block:test', date), id: itemId, date, tag: 'test:' + Date.now() };
    if (inMinutes === 0) {
      const r = await sendToAll(payload);
      if (r.sent) await setJSON(K.lastPush, { id: payload.id, date: payload.date, url: payload.url, title: payload.title, at: Date.now() });
      return json({ ok: true, immediate: true, ...r, payload });
    }
    const q = await getJSON(K.oneoff, []);
    const one = { id: Math.random().toString(36).slice(2, 8), at: Date.now() + inMinutes * 60000, title: payload.title, body: payload.body, url: payload.url, itemId, date };
    q.push(one); await setJSON(K.oneoff, q);
    return json({ ok: true, scheduled: one, landsAt: new Date(one.at).toISOString() });
  }

  // ---- run the scheduler now (debugging)
  if (path === '/tick' && m === 'POST') return json(await runTick());
  if (path === '/tick-log' && m === 'GET') return json(await getJSON(K.log, []));
  if (path === '/preview-due' && m === 'GET') {
    const { days } = await loadDays(Date.now());
    const out = [];
    for (const day of days) for (const it of day.items) { if (it.notify) out.push({ id: it.id, at: new Date(it.startAt - it.lead * 60000).toISOString(), title: it.title }); for (const s of it.steps || []) if (s.notify) out.push({ id: s.id, at: new Date(s.startAt - s.lead * 60000).toISOString(), title: s.title, step: true }); }
    return json(out.sort((a, b) => a.at.localeCompare(b.at)));
  }

  // ---- journal (private text, Intention's own store)
  if (path === '/journal' && m === 'GET') {
    const date = url.searchParams.get('date') || today;
    if (!DATE_RE.test(date)) return bad('date must be YYYY-MM-DD');
    return json(await getJSON(K.journal(date), {}));
  }
  if (path === '/journal' && m === 'POST') {
    const b = await readBody(req);
    if (!b || !DATE_RE.test(b.date || '') || !b.part || typeof b.data !== 'object') return bad('need {date, part, data}');
    const key = K.journal(b.date);
    const cur = await getJSON(key, {});
    cur[b.part] = { ...(cur[b.part] || {}), ...b.data, savedAt: Date.now() };
    await setJSON(key, cur);
    return json({ ok: true, journal: cur });
  }

  // ---- the three write-backs into the training app (BLUE LIST, SCOREBOARD, 50/50 Log)
  if (path === '/writeback' && m === 'POST') {
    const b = await readBody(req);
    if (!b || !b.table) return bad('need {table, row} or {table, rows}');
    try { return json(await writeBack(b)); } catch (e) { return bad(String(e && e.message || e), 502); }
  }

  return bad('not found', 404);
};

export const config = { path: ['/api', '/api/*'] };
