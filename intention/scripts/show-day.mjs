#!/usr/bin/env node
// Print one day the way Intention runs it. Usage:
//   node scripts/show-day.mjs [YYYY-MM-DD] [--snapshot path.json] [--drafts]
// Reads the live snapshot from $TRAINING_SYNC_URL unless --snapshot is given.
import { readFileSync } from 'node:fs';
import { decodeSnapshot, buildDay, nowParts, gridDateFor, addDays, flatten } from '../lib/day.mjs';

const args = process.argv.slice(2);
const flag = (n) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : null; };
const content = JSON.parse(readFileSync(new URL('../content/items.json', import.meta.url)));
const snapPath = flag('--snapshot');
let snap;
if (snapPath) snap = JSON.parse(readFileSync(snapPath, 'utf8'));
else {
  const url = process.env.TRAINING_SYNC_URL;
  if (!url) { console.error('set TRAINING_SYNC_URL or pass --snapshot'); process.exit(2); }
  snap = await (await fetch(url)).json();
}
const parsed = decodeSnapshot(snap);
const dateArg = args.find(a => /^\d{4}-\d{2}-\d{2}$/.test(a));
const date = dateArg || addDays(gridDateFor(nowParts()), 1);
const day = buildDay(parsed, date, content, {}, {});

const GOAL = { HEALTH: 'HEALTH', BASKETBALL: 'BALL', CLASSROOM: 'CLASS', MONEY: 'MONEY' };
console.log(`\n${day.dayName} ${day.date}  (grid col ${day.col}, snapshot rev ${day.rev})`);
console.log(`card: ${day.card.join(' · ')}\n`);
for (const it of day.items) {
  const bell = it.notify ? `🔔${it.lead ? '-' + it.lead : ''}` : '  ';
  console.log(`${it.startShort.padStart(5)}–${it.endShort.padEnd(5)} ${bell} [${GOAL[it.goal]}] ${it.title}${it.draft ? '  (draft)' : ''}`);
  for (const s of it.steps) {
    const b = s.notify ? '🔔' : '  ';
    const tool = s.tool && s.tool !== 'none' && s.tool !== 'page' ? ` {${s.tool}}` : '';
    console.log(`        ${s.startShort.padStart(5)} ${b} [${GOAL[s.goal]}] ${s.title}${tool}${s.page ? ' →' + s.page.title : ''}${s.draft ? ' (draft)' : ''}`);
  }
}
if (args.includes('--drafts')) {
  console.log('\n=== DRAFTS ===');
  for (const x of flatten(day)) if (x.draft) console.log(`- ${x.id}\n    goal ${x.goal} · how: ${x.how || '—'}\n    why: ${x.why || '—'}`);
}
