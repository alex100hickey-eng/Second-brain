// The scheduler's brain: what is due right now, and sending it.
// Rules (from the brief): land within about a minute; skip if Done; nothing is
// ever "missed" — anything more than LATE_MS old just drops.
import content from '../../../content/items.json' with { type: 'json' };
import { decodeSnapshot, buildDay, nowParts, gridDateFor, addDays, isDone, TZ } from '../../../lib/day.mjs';
import { getJSON, setJSON, K } from './store.mjs';
import { fetchSnapshot } from './snapshot.mjs';
import { sendToAll } from './push.mjs';

export const LATE_MS = 3 * 60 * 1000;   // a push older than this is dropped, not sent late
const MAX_PER_TICK = 3;

export function itemUrl(id, date) { return `/#/item/${encodeURIComponent(id)}?d=${date}`; }

export function payloadFor(x, parent, date) {
  const first = !parent && x.steps && x.steps.length ? x.steps[0] : null;
  let body = '';
  if (first) body = `First: ${first.title}`;
  else if (x.how) body = x.how;
  else if (x.why) body = x.why;
  if (body.length > 140) body = body.slice(0, 137) + '…';
  const time = x.startShort;
  return { title: x.title, body: body || time, tag: `${date}:${x.id}`, url: itemUrl(x.id, date), id: x.id, date, goal: x.goal };
}

/** Pure: which things fire now. `days` = built days, `doneBy`/`sentBy` = maps by date. */
export function findDue(days, doneBy, sentBy, now) {
  const due = [];
  for (const day of days) {
    const done = doneBy[day.date] || {}, sent = sentBy[day.date] || {};
    const consider = (x, parent, idx) => {
      if (!x.notify) return;
      if (sent[x.id]) return;
      if (isDone(x, parent, done)) return;
      if (parent) {
        if (idx === 0) return;                                   // the block's push names the first step
        if (!isDone(parent.steps[idx - 1], parent, done)) return; // chain: a step pings once the one before it is done
        if (parent.steps.slice(idx + 1).some(s => done[s.id])) return; // he is already past it
      }
      const fireAt = x.startAt - (x.lead || 0) * 60000;
      if (fireAt <= now && now - fireAt < LATE_MS) due.push({ x, parent, date: day.date, fireAt });
    };
    for (const item of day.items) {
      consider(item, null, 0);
      (item.steps || []).forEach((s, i) => consider(s, item, i));
    }
  }
  due.sort((a, b) => a.fireAt - b.fireAt);
  return due;
}

export async function loadDays(now) {
  const parts = nowParts(TZ, new Date(now));
  const colDate = gridDateFor(parts);
  // yesterday's column (its tail runs past midnight), today's, and tomorrow's (an
  // early item with lead time can fire before 3 AM flips the column)
  const cols = [addDays(colDate, -1), colDate, addDays(colDate, 1)];
  const [{ snap, fromCache }, overrides, settings] = await Promise.all([
    fetchSnapshot(), getJSON(K.overrides, {}), getJSON(K.settings, {}),
  ]);
  const parsed = decodeSnapshot(snap);
  const days = cols.map(d => buildDay(parsed, d, content, overrides, settings));
  return { days, colDate, parsed, fromCache, overrides, settings };
}

/** One tick. Returns a small report (also appended to the tick log). */
export async function runTick(now = Date.now()) {
  const report = { at: new Date(now).toISOString(), sent: [], skipped: 0, errors: [] };
  try {
    const { days, colDate, fromCache } = await loadDays(now);
    report.date = colDate; report.fromCache = fromCache;
    const dates = days.map(d => d.date);
    const doneBy = {}, sentBy = {};
    await Promise.all(dates.map(async d => { doneBy[d] = await getJSON(K.done(d), {}); sentBy[d] = await getJSON(K.sent(d), {}); }));
    const due = findDue(days, doneBy, sentBy, now);
    const oneoff = await getJSON(K.oneoff, []);
    const oneDue = oneoff.filter(o => o.at <= now && now - o.at < LATE_MS);
    const oneStale = oneoff.filter(o => now - o.at >= LATE_MS);
    const subs = await getJSON(K.subs, {});
    report.subs = Object.keys(subs).length;
    const queue = [
      ...oneDue.map(o => ({ payload: { title: o.title, body: o.body, tag: 'oneoff:' + o.id, url: o.url, id: o.itemId, date: o.date }, one: o })),
      ...due.slice(0, MAX_PER_TICK).map(d => ({ payload: payloadFor(d.x, d.parent, d.date), due: d })),
    ];
    report.skipped = Math.max(0, due.length - MAX_PER_TICK);
    if (queue.length && report.subs) {
      let lastPush = null;
      for (const q of queue) {
        const r = await sendToAll(q.payload, subs);
        report.sent.push({ title: q.payload.title, id: q.payload.id, ...r });
        if (r.errors && r.errors.length) report.errors.push(...r.errors);
        if (r.sent) lastPush = { id: q.payload.id, date: q.payload.date, url: q.payload.url, title: q.payload.title, at: now };
      }
      // iOS may launch the app at its start page when a notification is tapped, ignoring the
      // service worker; the app asks "what did you just ping?" and opens that item itself.
      if (lastPush) await setJSON(K.lastPush, lastPush);
      // record sends so the next minute does not repeat them
      for (const d of due.slice(0, MAX_PER_TICK)) { sentBy[d.date][d.x.id] = now; }
      await Promise.all(dates.map(d => setJSON(K.sent(d), sentBy[d])));
    } else if (queue.length) {
      report.note = 'due but no subscriptions yet';
    }
    if (oneDue.length || oneStale.length) {
      const keep = oneoff.filter(o => !oneDue.includes(o) && !oneStale.includes(o));
      await setJSON(K.oneoff, keep);
    }
    report.dueCount = due.length;
  } catch (e) {
    report.errors.push(String(e && e.stack || e));
  }
  try {
    const log = await getJSON(K.log, []);
    log.unshift(report); if (log.length > 60) log.length = 60;
    await setJSON(K.log, log);
  } catch { /* logging must never break the tick */ }
  return report;
}
