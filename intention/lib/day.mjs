// lib/day.mjs — the engine. Turns the training app's live snapshot into the day
// Intention runs: blocks from the grid, steps inside them, each with what / how /
// why / goal / tool / notify. Pure functions, no I/O. All times are wall-clock
// America/New_York so DST days do not drift.
//
// Grid semantics (from the training app): 48 slots of 30 min starting 3:00 AM,
// columns Sun=0..Sat=6, cell key "slot|day". A column runs 3 AM -> 3 AM, so the
// "date" of a day here is the date of its 3 AM start.

export const TZ = 'America/New_York';
export const DAY_START_MIN = 3 * 60;
export const SLOT_MIN = 30;
export const N_SLOTS = 48;
export const GOALS = ['HEALTH', 'BASKETBALL', 'CLASSROOM', 'MONEY'];
export const CATS = ['Lifts', 'Good Drills', 'Bag shooting', '50/50', 'Conditioning',
  'Group workouts', 'Court movement', 'IQ'];
const DAY_ABBR = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

// ---------------------------------------------------------------- wall clock
export function nowParts(tz = TZ, d = new Date()) {
  const f = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const p = Object.fromEntries(f.formatToParts(d).map(x => [x.type, x.value]));
  return { y: +p.year, m: +p.month, d: +p.day, hh: +p.hour % 24, mm: +p.minute, ss: +p.second };
}
export function iso(y, m, d) {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}
export function parseIso(s) { const [y, m, d] = s.split('-').map(Number); return { y, m, d }; }
export function addDays(isoDate, n) {
  const { y, m, d } = parseIso(isoDate);
  const x = new Date(Date.UTC(y, m - 1, d + n));
  return iso(x.getUTCFullYear(), x.getUTCMonth() + 1, x.getUTCDate());
}
export function dayIndex(isoDate) { // Sun=0, matches the grid's columns
  const { y, m, d } = parseIso(isoDate);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}
export function weekStart(isoDate) { return addDays(isoDate, -dayIndex(isoDate)); }
/** The grid column (as its date) that contains this wall-clock moment. Before 3 AM
 *  you are still inside yesterday's column. */
export function gridDateFor(parts) {
  const today = iso(parts.y, parts.m, parts.d);
  return parts.hh * 60 + parts.mm < DAY_START_MIN ? addDays(today, -1) : today;
}
/** Minutes since midnight of a column's date -> epoch ms, honouring the zone's
 *  offset at that instant (two passes handle DST transitions). */
export function zonedEpoch(isoDate, minutes, tz = TZ) {
  const { y, m, d } = parseIso(isoDate);
  const wall = Date.UTC(y, m - 1, d, 0, minutes);
  let guess = wall;
  for (let i = 0; i < 2; i++) {
    const p = nowParts(tz, new Date(guess));
    const seen = Date.UTC(p.y, p.m - 1, p.d, p.hh, p.mm, p.ss);
    guess = wall - (seen - guess);
  }
  return guess;
}
/** Column-frame minutes (may exceed 1440 for the post-midnight tail) -> wall clock. */
export function clockOf(isoDate, minutes) {
  const dayOff = Math.floor(minutes / 1440);
  const m = minutes - dayOff * 1440;
  const hh = Math.floor(m / 60), mm = m % 60;
  const h12 = hh % 12 || 12;
  return {
    date: addDays(isoDate, dayOff), hh, mm,
    label: `${h12}:${String(mm).padStart(2, '0')} ${hh < 12 ? 'AM' : 'PM'}`,
    short: `${h12}:${String(mm).padStart(2, '0')}`,
  };
}
export function minutesNow(parts, colDate) {
  const today = iso(parts.y, parts.m, parts.d);
  const dayOff = today === colDate ? 0 : (addDays(colDate, 1) === today ? 1 : null);
  if (dayOff === null) return null;
  return dayOff * 1440 + parts.hh * 60 + parts.mm;
}

// ---------------------------------------------------------------- snapshot
export function decodeSnapshot(snap) {
  const K = (snap && snap.keys) || {};
  const J = (k, fb) => { try { const v = K[k]; return v == null ? fb : JSON.parse(v); } catch { return fb; } };
  return {
    rev: (snap && snap.rev) || null,
    grid: J('weeklySchedule_v1', {}) || {},
    once: J('weeklyOnce_v1', null),
    obligations: J('bigObligations_v1', []) || [],
    routines: J('dailyRoutines_v1', {}) || {},
    workouts: J('weeklyWorkouts_v1', {}) || {},
    warmup: J('warmupRoutine_v1', '') || '',
    library: J('workoutLibrary_v2', {}) || {},
  };
}
export function effectiveGrid(parsed, colDate) {
  const g = { ...(parsed.grid || {}) };
  const once = parsed.once;
  if (once && once.weekStart && once.cells) {
    const ws = once.weekStart, we = addDays(ws, 7);
    if (ws <= colDate && colDate < we) {
      for (const [k, v] of Object.entries(once.cells)) if (typeof v === 'string' && v.trim()) g[k] = v;
    }
  }
  return g;
}
export function rawBlocks(parsed, colDate) {
  const col = dayIndex(colDate), g = effectiveGrid(parsed, colDate);
  const cells = [];
  for (const [k, v] of Object.entries(g)) {
    const [s, d] = k.split('|').map(Number);
    if (d === col && s >= 0 && s < N_SLOTS && typeof v === 'string' && v.trim()) cells.push([s, v.trim()]);
  }
  cells.sort((a, b) => a[0] - b[0]);
  const blocks = [];
  for (const [s, v] of cells) {
    const last = blocks[blocks.length - 1];
    if (last && last.text === v && last.slotEnd === s) last.slotEnd = s + 1;
    else blocks.push({ slotStart: s, slotEnd: s + 1, text: v });
  }
  return blocks;
}

// ---------------------------------------------------------------- times in text
// Grid cells carry exact times ("MATH 120 · 9:20–10:10"); the 30-min slot is the
// coarse frame. Use the text's times when present, the slot otherwise.
const T_RE = /(\d{1,2}):(\d{2})/g;
function resolve(h, mm, ref, after) {
  let cands = [(h % 12) * 60 + mm, ((h % 12) + 12) * 60 + mm].map(c => (c < DAY_START_MIN ? c + 1440 : c));
  if (after != null) { const ok = cands.filter(c => c > after); if (ok.length) cands = ok; }
  cands.sort((a, b) => Math.abs(a - ref) - Math.abs(b - ref));
  return cands[0];
}
export function blockTimes(text, slotStart, slotEnd, prevEnd) {
  const s0 = DAY_START_MIN + slotStart * SLOT_MIN, e0 = DAY_START_MIN + slotEnd * SLOT_MIN;
  const found = [...text.matchAll(T_RE)].map(m => ({
    h: +m[1], mm: +m[2], before: text.slice(Math.max(0, m.index - 8), m.index).toLowerCase(),
  }));
  let start = s0, end = e0, explicitStart = false;
  if (found.length) {
    const f = found[0], l = found[found.length - 1];
    const deadline = /(\bby|\barrive|→)\s*$/.test(f.before);
    if (!deadline) { start = resolve(f.h, f.mm, s0); explicitStart = true; }
    if (found.length > 1) end = resolve(l.h, l.mm, e0, start);
    else if (deadline && /arrive/.test(f.before)) end = resolve(f.h, f.mm, e0, start);
  }
  if (!explicitStart && prevEnd != null && prevEnd > start && prevEnd < end) start = prevEnd;
  if (end <= start) end = e0 > start ? e0 : start + SLOT_MIN;
  return { start, end, explicitStart };
}
/** Times in a dated calendar line ("Practice 4:00–5:50", "GAME 7:00 PM", "ACCT Final 8:00-11:00 AM").
 *  No slot to anchor AM/PM, so: an explicit am/pm marker wins; otherwise 7–11 is morning, 12 and 1–6
 *  afternoon/evening. Returns null when the line has no time. */
export function calendarTimes(text) {
  const re = /(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?/gi;
  const found = [...text.matchAll(re)].map(m => ({ h: +m[1], mm: +m[2], ap: (m[3] || '').replace(/\./g, '').toLowerCase() }));
  if (!found.length) return null;
  // a trailing am/pm applies to every time before it ("8:00-11:00 AM")
  const lastAp = [...found].reverse().find(f => f.ap);
  const toMin = (f) => {
    const ap = f.ap || (lastAp ? lastAp.ap : '');
    let h = f.h % 12;
    if (ap === 'pm') h += 12;
    else if (!ap && (f.h === 12 || f.h <= 6)) h += 12;   // daytime default
    let v = h * 60 + f.mm;
    if (v < DAY_START_MIN) v += 1440;
    return v;
  };
  const start = toMin(found[0]);
  let end = found.length > 1 ? toMin(found[found.length - 1]) : start + 60;
  if (end <= start) end = start + 60;
  return { start, end };
}
export function cleanTitle(text) {
  return text
    .replace(/\s*\b(by|arrive)\s+\d{1,2}:\d{2}/gi, '')
    .replace(/\s*\d{1,2}:\d{2}(\s*[–—\-→]\s*\d{1,2}:\d{2})?/g, '')
    .replace(/\s*[·•]\s*[·•]\s*/g, ' · ')
    .replace(/\s*[·•]\s*$/, '')
    .replace(/^\s*[·•]\s*/, '')
    .replace(/\s+→\s*$/, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

// ---------------------------------------------------------------- ids + kinds
export function norm(s) {
  return String(s).replace(/\d{1,2}:\d{2}/g, ' ').replace(/[→–—]/g, ' ').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}
export function kindOf(text) {
  const t = text.toLowerCase();
  if (/\bsleep\b/.test(t)) return 'sleep';
  if (/night routine/.test(t)) return 'routine-pm';
  if (/^wake\b|morning routine/.test(t)) return 'routine-am';
  if (/50\s*\/\s*50/.test(t)) return 'fifty';
  if (/\bpractice\b|\bgame\b|\bscrimmage\b/.test(t)) return 'gameday';
  if (/^gym\b/.test(t)) {
    if (/warm\s*up|warmup|cars|crawls/.test(t)) return 'gym-warmup';
    if (/good drills/.test(t)) return 'gym-morning';
    return 'gym-main';
  }
  if (/\b[A-Z]{4}\s?\d{3}\b/.test(text) || /\blab\b|\blecture\b/.test(t)) return 'class';
  if (/breakfast|lunch|dinner|snack|\beat\b/.test(t)) return 'meal';
  if (/study|review|homework|knock out|\bwork\b/.test(t)) return 'study';
  if (/^class\b/.test(t)) return 'class';   // "get ready for class" is a transition, "Class · Sears 439" is a class
  if (/to gym|back to dorm|arrive|get ready|clean up|walk/.test(t)) return 'transition';
  if (/\bgym\b/.test(t)) return 'gym-main';
  return 'other';
}
/** Is x done, given the day's done map? A block with steps counts as done when
 *  every step is; a step counts as done when its block was marked done. */
export function isDone(x, parent, done) {
  if (!done) return false;
  if (done[x.id]) return true;
  if (parent && done[parent.id]) return true;
  if (x.steps && x.steps.length) return x.steps.every(s => done[s.id]);
  return false;
}
export const GYM_KINDS = new Set(['gym-warmup', 'gym-morning', 'gym-main']);

// ---------------------------------------------------------------- routines + cards
export function routineSteps(text) {
  const steps = []; let group = null;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) { group = null; continue; }
    if (group) { group.checklist.push(line); continue; }
    if (/:$/.test(line)) { group = { title: line.replace(/:$/, ''), checklist: [] }; steps.push(group); continue; }
    steps.push({ title: line, checklist: null });
  }
  return steps;
}
export function cardLines(card) {
  return String(card || '').split('\n').map(l => l.replace(/^[\s•\-*]+/, '').trim()).filter(Boolean);
}
const MORNING_SET = /^(movement|good handles|good handling|good shooting|good finishing)$/i;
const SHOOTING = /dribble bag|on the move|good shooting|good finishing|50\/50/i;

// ---------------------------------------------------------------- library
export function allPages(library) {
  const out = [];
  for (const [c, cat] of Object.entries(library || {})) {
    (cat && cat.pages || []).forEach((p, i) => out.push({ cat: +c, catName: CATS[+c] || `Tab ${c}`, index: i, ...p }));
  }
  return out;
}
export function findPage(library, title) {
  if (!title) return null;
  const want = title.toLowerCase().trim();
  const pages = allPages(library);
  return pages.find(p => (p.title || '').toLowerCase().trim() === want)
    || pages.find(p => (p.title || '').toLowerCase().startsWith(want))
    || pages.find(p => (p.title || '').toLowerCase().includes(want))
    || null;
}
const ALIASES = {
  'good handles': 'Good Handling', 'movement': 'Movement - Drive Position',
  'reads': 'READS - The Daily Block', 'recording': 'RECORDING - Does It Sell',
  '50/50': '50/50 - The Standard', 'dribble bag': 'DRIBBLE BAG',
  'on the move': 'ON THE MOVE - The Shooting Block', 'conditioning': 'Conditioning',
  'compete': 'Compete + Fatigue', 'defense': 'Defense', 'passing': 'Passing',
};
const LIFT_WORDS = /lower|upper|heavy|strength|mobility|dunks|sprints|\blift\b|rotation|lengthened|pull|trunk/i;
export function liftPageFor(library, colDate) {
  const abbr = DAY_ABBR[dayIndex(colDate)];
  return allPages(library).filter(p => p.cat === 0)
    .find(p => (p.title || '').split(' - ')[0].split(/[^A-Za-z]+/).includes(abbr)) || null;
}
function halfBody(body, half) {
  const lines = String(body || '').split('\n');
  const content = lines.filter(l => l.trim());
  if (content.length < 4) return body;
  const mid = Math.ceil(content.length / 2);
  const pick = half === 'a' ? content.slice(0, mid) : content.slice(mid);
  return pick.join('\n');
}
export function pageForDrill(parsed, line, colDate) {
  const base = line.replace(/\s*-\s*half\s+[ab]\s*$/i, '').trim();
  const half = ((line.match(/half\s+([ab])/i) || [])[1] || '').toLowerCase();
  if (/^vitamins$/i.test(base)) return { title: 'VITAMINS', body: parsed.warmup, catName: 'Everyday warmup', half: '' };
  let p = findPage(parsed.library, ALIASES[base.toLowerCase()] || base);
  if (!p && LIFT_WORDS.test(base)) p = liftPageFor(parsed.library, colDate);
  if (!p) return null;
  const out = { title: p.title, catName: p.catName, type: p.type || 'text', half };
  if (p.type === 'table') { out.columns = p.columns; out.rows = p.rows; }
  else out.body = half ? halfBody(p.body, half) : p.body;
  return out;
}
export function pageByTitle(parsed, title) {
  const p = findPage(parsed.library, title);
  if (!p) return null;
  const out = { title: p.title, catName: p.catName, type: p.type || 'text' };
  if (p.type === 'table') { out.columns = p.columns; out.rows = p.rows; } else out.body = p.body;
  return out;
}

// ---------------------------------------------------------------- content merge
const FIELDS = ['goal', 'how', 'why', 'tool', 'notify', 'lead', 'duration', 'draft', 'source', 'title', 'guide'];
function pick(o) {
  const r = {};
  if (o) for (const f of FIELDS) if (o[f] !== undefined) r[f] = o[f];
  return r;
}
/** A guide is the in-depth sheet: { title, intro, steps: [{ title, do, intention }], whole, remember, source }.
 *  A string names a shared entry in content.guides. */
function resolveGuide(g, content) {
  if (!g) return null;
  if (typeof g === 'string') { const shared = content && content.guides && content.guides[g]; return shared && typeof shared === 'object' ? shared : null; }
  return typeof g === 'object' && Array.isArray(g.steps) ? g : null;
}
export function resolveContent(id, kind, content, overrides) {
  const kinds = (content && content.kinds) || {};
  const items = (content && content.items) || {};
  const base = { goal: 'HEALTH', how: '', why: '', tool: 'none', notify: false, lead: 0, draft: true, source: '' };
  const merged = { ...base, ...pick(kinds[kind]), ...pick(items[id]), ...pick(overrides && overrides[id]) };
  merged.edited = !!(overrides && overrides[id]);
  merged.guide = resolveGuide(merged.guide, content);
  return merged;
}

// ---------------------------------------------------------------- build the day
function mk(base, colDate, start, end, c) {
  const s = clockOf(colDate, start), e = clockOf(colDate, end);
  return {
    ...base, start, end,
    startAt: zonedEpoch(colDate, start), endAt: zonedEpoch(colDate, end),
    startLabel: s.label, endLabel: e.label, startShort: s.short, endShort: e.short,
    goal: c.goal, how: c.how, why: c.why, tool: c.tool, notify: !!c.notify, lead: +c.lead || 0,
    draft: !!c.draft, source: c.source || '', edited: !!c.edited, guide: c.guide || null,
  };
}

function stepsFor(item, parsed, card, colDate, ctx, content, overrides) {
  const steps = [];
  const push = (id, kind, title, extra = {}) => steps.push({ id, kind, title, ...extra });
  if (item.kind === 'routine-am' || item.kind === 'routine-pm') {
    const which = item.kind === 'routine-am' ? 'morning' : 'night';
    const pre = which === 'morning' ? 'am' : 'pm';
    for (const s of routineSteps(parsed.routines[which])) {
      push(`${pre}:${norm(s.title)}`, 'step', s.title, { checklist: s.checklist });
    }
  } else if (item.kind === 'gym-warmup') {
    for (const part of cleanTitle(item.title).split(/[·/]/).map(x => x.trim()).filter(x => x && !/^gym$/i.test(x))) {
      if (/warm\s*up/i.test(part)) push('drill:vitamins', 'drill', 'Vitamins', { page: pageForDrill(parsed, 'Vitamins', colDate) });
      else push(`drill:${norm(part)}`, 'drill', part);
    }
  } else if (item.kind === 'gym-morning') {
    for (const line of card.filter(l => MORNING_SET.test(l))) {
      push(`drill:${norm(line)}`, 'drill', line, { page: pageForDrill(parsed, line, colDate) });
    }
  } else if (item.kind === 'gym-main') {
    const lines = card.filter(l => {
      if (/^vitamins$/i.test(l)) return false;
      if (/50\s*\/\s*50/.test(l)) return !ctx.hasFifty;
      if (MORNING_SET.test(l)) return !ctx.hasMorningGym;
      return true;
    });
    for (const line of lines) push(`drill:${norm(line)}`, 'drill', line, { page: pageForDrill(parsed, line, colDate) });
    if (lines.some(l => SHOOTING.test(l))) push('drill:stakes-set', 'drill', 'Stakes set', { page: pageByTitle(parsed, 'PRESSURE') });
  } else if (item.kind === 'gameday') {
    const gd = pageByTitle(parsed, 'GAME DAY');
    push('gd:before', 'gd', 'BEFORE — 2 minutes, seated', { page: gd });
    push('gd:door', 'gd', 'THE DOOR', { page: gd });
    push('gd:after', 'gd', 'AFTER — best proof to the BLUE LIST', { page: gd });
  }
  // planned times: block start + earlier steps' durations
  const total = item.end - item.start;
  const fixed = steps.map(s => resolveContent(s.id, s.kind, content, overrides));
  const known = fixed.reduce((a, c) => a + (c.duration > 0 ? +c.duration : 0), 0);
  const unknownN = fixed.filter(c => !(c.duration > 0)).length;
  const fill = unknownN ? Math.max(1, Math.floor(Math.max(0, total - known) / unknownN)) : 0;
  let cursor = item.start;
  return steps.map((s, i) => {
    const c = fixed[i];
    const dur = c.duration > 0 ? +c.duration : fill;
    const out = mk({ id: s.id, kind: s.kind, title: c.title || s.title, parentId: item.id, checklist: s.checklist || null, page: s.page || null }, colDate, cursor, cursor + dur, c);
    out.duration = dur; out.durationDraft = !(c.duration > 0);
    cursor += dur;
    return out;
  });
}

/**
 * Build one grid day. `content` = content/items.json, `overrides` = per-item edits
 * from Intention's own store, `settings` = weekly placements.
 */
export function buildDay(parsed, colDate, content = {}, overrides = {}, settings = {}) {
  const blocks = rawBlocks(parsed, colDate);
  const col = dayIndex(colDate);
  const card = cardLines(parsed.workouts[col]);
  const kinds = blocks.map(b => kindOf(b.text));
  const ctx = { hasMorningGym: kinds.includes('gym-morning'), hasFifty: kinds.includes('fifty') };
  const items = [];
  let prevEnd = null;
  blocks.forEach((b, i) => {
    const { start, end } = blockTimes(b.text, b.slotStart, b.slotEnd, prevEnd);
    prevEnd = end;
    const kind = kinds[i];
    const id = `block:${norm(cleanTitle(b.text))}`;
    const c = resolveContent(id, kind, content, overrides);
    const item = mk({ id, kind, title: c.title || cleanTitle(b.text), raw: b.text }, colDate, start, end, c);
    item.steps = stepsFor(item, parsed, card, colDate, ctx, content, overrides);
    if (GYM_KINDS.has(kind)) item.card = card;
    items.push(item);
  });
  // Dated calendar items ("Practice 4:00–5:50") for this column's date.
  for (const o of parsed.obligations || []) {
    if (!o || o.date !== colDate || !o.text) continue;
    for (const line of String(o.text).split('\n').map(x => x.trim()).filter(Boolean)) {
      const kind = kindOf(line);
      const id = `cal:${norm(line)}`;
      const c = resolveContent(id, kind === 'gameday' ? 'gameday' : 'calendar', content, overrides);
      const ct = calendarTimes(line);
      const item = mk({ id, kind: kind === 'gameday' ? 'gameday' : 'calendar', title: cleanTitle(line), raw: line, allDay: !ct }, colDate, ct ? ct.start : DAY_START_MIN, ct ? ct.end : DAY_START_MIN + 30, c);
      item.steps = kind === 'gameday' ? stepsFor(item, parsed, card, colDate, ctx, content, overrides) : [];
      items.push(item);
    }
  }
  // Weekly items he placed in settings: {pressureTest:{day,time}, hpv:[{day,time}], alarms:[{day|'*',time}], bolt:{day,time}}
  const weekly = weeklyItems(settings, colDate, content, overrides, parsed);
  items.push(...weekly);
  // The 6 R's: a few times a day at random, never in class or practice (his ask, 2026-09-23).
  items.push(...sixRItems(items, colDate, content, overrides, settings));
  items.sort((a, b) => a.start - b.start || (a.allDay ? -1 : 1));
  return { date: colDate, dayName: DAY_NAMES[col], col, card, items, rev: parsed.rev };
}

// Seeded randomness so the app and the scheduler agree on today's times.
function hash32(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function mulberry32(a) { return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

/** 6 R's reminders: `count` per day (default 4), random 5-minute marks between the end of the
 *  morning routine and 30 min before sleep, at least an hour apart, never inside a class or a
 *  practice/game (10 min margin). Same times all day for a given date. */
export function sixRItems(items, colDate, content, overrides, settings) {
  const cfg = (settings && settings.sixr) || {};
  const count = cfg.count === undefined || cfg.count === '' || cfg.count === null ? 4 : Math.max(0, Math.min(12, +cfg.count || 0));
  if (!count) return [];
  const blocks = items.filter(i => !i.weekly && !i.allDay);
  if (!blocks.length) return [];
  const wake = blocks.find(b => b.kind === 'routine-am');
  const sleep = blocks.find(b => b.kind === 'sleep');
  const startMin = wake ? wake.end : blocks[0].start;
  const endMin = sleep ? sleep.start - 30 : Math.max(...blocks.map(b => b.end)) - 30;
  if (endMin - startMin < 60) return [];
  const busy = blocks.filter(b => b.kind === 'class' || b.kind === 'gameday').map(b => [b.start - 10, b.end + 10]);
  const rng = mulberry32(hash32(colDate + '|sixr|' + count));
  const picks = [];
  for (let tries = 0; tries < 1000 && picks.length < count; tries++) {
    const t = Math.round((startMin + rng() * (endMin - startMin)) / 5) * 5;
    if (t < startMin || t > endMin) continue;
    if (busy.some(([a, b]) => t >= a && t < b)) continue;
    if (picks.some(p => Math.abs(p - t) < 60)) continue;
    picks.push(t);
  }
  picks.sort((a, b) => a - b);
  return picks.map((t, i) => {
    const id = `sixr:${i + 1}`;
    const c = resolveContent(id, 'sixr', content, overrides);
    return { ...mk({ id, kind: 'sixr', title: c.title || "6 R's", weekly: true, auto: true }, colDate, t, t + 1, c), steps: [] };
  });
}

function weeklyItems(settings, colDate, content, overrides, parsed) {
  const out = [];
  const col = dayIndex(colDate);
  const toMin = (t) => { const m = /^(\d{1,2}):(\d{2})$/.exec(t || ''); if (!m) return null; let v = (+m[1]) * 60 + (+m[2]); if (v < DAY_START_MIN) v += 1440; return v; };
  const add = (id, kind, title, time, dur, extra = {}) => {
    const start = toMin(time); if (start == null) return;
    const c = resolveContent(id, kind, content, overrides);
    out.push({ ...mk({ id, kind, title: c.title || title, weekly: true, ...extra }, colDate, start, start + dur, c), steps: [] });
  };
  const onDay = (d) => d === '*' || d === 'daily' || +d === col;
  const s = settings || {};
  if (s.pressureTest && onDay(s.pressureTest.day)) add('weekly:pressure-test', 'weekly', 'PRESSURE TEST', s.pressureTest.time, 15, { page: pageByTitle(parsed, 'PRESSURE') });
  for (const h of s.hpv || []) if (onDay(h.day)) add('weekly:hpv', 'weekly', 'HIGH PRESSURE VISUALIZATION', h.time, 10, { page: pageByTitle(parsed, 'HIGH PRESSURE VISUALIZATION') });
  (s.alarms || []).forEach((a, i) => { if (onDay(a.day)) add(`weekly:belief-alarm-${i + 1}`, 'alarm', a.title || `Belief alarm ${i + 1}`, a.time, 1); });
  if (s.bolt && onDay(s.bolt.day)) add('weekly:bolt', 'weekly', 'BOLT', s.bolt.time, 3);
  return out;
}

/** Flatten a day into every notifiable thing with its planned time. */
export function flatten(day) {
  const out = [];
  for (const it of day.items) { out.push(it); for (const s of it.steps || []) out.push(s); }
  return out;
}
