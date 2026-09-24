import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { blockTimes, calendarTimes, cleanTitle, kindOf, norm, decodeSnapshot, buildDay, gridDateFor, zonedEpoch, routineSteps, isDone } from '../lib/day.mjs';

// slot 8 = 7:00 AM (3 AM + 8*30min), slot 22 = 2:00 PM, slot 38 = 10:00 PM
test('exact times in the cell text win over the slot', () => {
  assert.deepEqual(pick(blockTimes('Wake 7:00 · morning routine → 7:30', 8, 9)), [420, 450]);
  assert.deepEqual(pick(blockTimes('Breakfast 7:30–7:45 · to gym 7:45–7:55', 9, 10)), [450, 475]);
  assert.deepEqual(pick(blockTimes('MATH 120 · 9:20–10:10 · Sears 439', 12, 14)), [560, 610]);
  assert.deepEqual(pick(blockTimes('Lunch 12:45–1:20', 20, 21)), [765, 800]);
  assert.deepEqual(pick(blockTimes('Gym · 12:50–4:00', 20, 26)), [770, 960]);
  assert.deepEqual(pick(blockTimes('Gym · 3:00–7:00', 24, 32)), [900, 1140]);
});
test('single times: start unless it is a deadline', () => {
  assert.deepEqual(pick(blockTimes('Sleep 9:45 → wake', 38, 48)), [1305, 1620]);
  assert.deepEqual(pick(blockTimes('Night routine 10:00–10:40', 38, 39)), [1320, 1360]);
  assert.deepEqual(pick(blockTimes('Sleep 10:40 → wake', 39, 48)), [1360, 1620]);
  // "arrive 3:00" is an end; start follows the previous block's real end
  assert.deepEqual(pick(blockTimes('To gym · arrive 3:00', 23, 24, 880)), [880, 900]);
  // "by 9:15" is a deadline: keep the slot
  assert.deepEqual(pick(blockTimes('Back to dorm by 9:15 · get ready for class', 12, 13, 540)), [540, 570]);
});
test('titles lose their times and ids are stable across days', () => {
  assert.equal(cleanTitle('Wake 7:00 · morning routine → 7:30'), 'Wake · morning routine');
  assert.equal(cleanTitle('Wake 6:30 · morning routine → 7:00'), 'Wake · morning routine');
  assert.equal(cleanTitle('ACCT 100 · 10:00–11:15 · PBL 201'), 'ACCT 100 · PBL 201');
  assert.equal(cleanTitle('To gym · arrive 3:00'), 'To gym');
  assert.equal(cleanTitle('Back to dorm by 9:15 · get ready for class'), 'Back to dorm · get ready for class');
  assert.equal(norm(cleanTitle('50/50 · 1:20–1:50')), '50-50');
});
test('kinds', () => {
  assert.equal(kindOf('Sleep 9:45 → wake'), 'sleep');
  assert.equal(kindOf('Wake 7:00 · morning routine → 7:30'), 'routine-am');
  assert.equal(kindOf('Back to dorm 8:45 · night routine'), 'routine-pm');
  assert.equal(kindOf('Breakfast 7:30–7:45 · to gym 7:45–7:55'), 'meal');
  assert.equal(kindOf('To gym · arrive 3:00'), 'transition');
  assert.equal(kindOf('Gym · Cars/Crawls/warmup 7:55–8:20'), 'gym-warmup');
  assert.equal(kindOf('Gym · good drills 8:20–9:00'), 'gym-morning');
  assert.equal(kindOf('Gym · afternoon session 2:30–5:00'), 'gym-main');
  assert.equal(kindOf('CSDS 101 Lab · 6:30–8:30 · Olin 304'), 'class');
  assert.equal(kindOf('Class review + study 1:50–2:40'), 'study');
  assert.equal(kindOf('50/50 · 1:20–1:50'), 'fifty');
  assert.equal(kindOf('Practice 4:00–5:50'), 'gameday');
});
test('calendar lines resolve to daytime unless told otherwise', () => {
  assert.deepEqual(pick(calendarTimes('Practice 4:00–5:50')), [960, 1070]);
  assert.deepEqual(pick(calendarTimes('GAME vs Cleveland State 7:00 PM (moved to Wed)')), [1140, 1200]);
  assert.deepEqual(pick(calendarTimes('ACCT Final 8:00-11:00 AM')), [480, 660]);
  assert.deepEqual(pick(calendarTimes('MATH Final 3:30-6:30 PM — DOUBLE FINAL DAY')), [930, 1110]);
  assert.deepEqual(pick(calendarTimes('CSDS Final Project Presentation 12:30 PM')), [750, 810]);
  assert.deepEqual(pick(calendarTimes('AIQS Final Paper due 11:59 PM')), [1439, 1499]);
  assert.equal(calendarTimes('ECON 103 Exam 1'), null);
  assert.equal(kindOf('Back to dorm by 9:15 · get ready for class'), 'transition');
  assert.equal(kindOf('Class · Sears 439'), 'class');
});
test('routine lines: a line ending in ":" groups the lines after it', () => {
  const s = routineSteps('Plan Tomorrow\n\nBanded stuff 10 per side:\na\nb\n\nNeck isos');
  assert.deepEqual(s.map(x => x.title), ['Plan Tomorrow', 'Banded stuff 10 per side', 'Neck isos']);
  assert.deepEqual(s[1].checklist, ['a', 'b']);
});
test('grid day boundary is 3 AM and epochs honour New York DST', () => {
  assert.equal(gridDateFor({ y: 2026, m: 9, d: 24, hh: 2, mm: 59 }), '2026-09-23');
  assert.equal(gridDateFor({ y: 2026, m: 9, d: 24, hh: 3, mm: 0 }), '2026-09-24');
  // 7:00 AM EDT = 11:00 UTC; 7:00 AM EST (after Nov 1 2026) = 12:00 UTC
  assert.equal(new Date(zonedEpoch('2026-09-24', 420)).toISOString(), '2026-09-24T11:00:00.000Z');
  assert.equal(new Date(zonedEpoch('2026-11-02', 420)).toISOString(), '2026-11-02T12:00:00.000Z');
  // 1:30 AM in the tail of a column = the next calendar date
  assert.equal(new Date(zonedEpoch('2026-09-24', 1440 + 90)).toISOString(), '2026-09-25T05:30:00.000Z');
});
test('the live Thursday builds with the expected shape', () => {
  const snap = JSON.parse(readFileSync(new URL('./fixtures/snapshot.json', import.meta.url)));
  const content = JSON.parse(readFileSync(new URL('../content/items.json', import.meta.url)));
  const day = buildDay(decodeSnapshot(snap), '2026-09-24', content, {}, {});
  const titles = day.items.map(i => i.title);
  assert.equal(day.dayName, 'Thursday');
  assert.equal(titles[0], 'Wake · morning routine');
  assert.equal(day.items[0].steps.length, 13);
  assert.equal(day.items.find(i => i.kind === 'sleep').steps.length, 0);
  assert.equal(day.items.find(i => i.kind === 'meal').steps.length, 0);
  const gym = day.items.find(i => i.id === 'block:gym');
  assert.deepEqual(gym.steps.map(s => s.title), ['Passing', 'Dribble Bag - Half B', 'Reads', 'Defense', 'Upper strength', 'Stakes set']);
  assert.equal(gym.steps[4].page.title, 'Thu - Heavy Upper');
  // notify defaults: routine + gym + 50/50 on, classes + meals off
  assert.equal(day.items.find(i => i.kind === 'class').notify, false);
  assert.equal(day.items.find(i => i.kind === 'fifty').notify, true);
  // overrides win
  const d2 = buildDay(decodeSnapshot(snap), '2026-09-24', content, { 'block:gym': { notify: false, why: 'mine' } }, {});
  assert.equal(d2.items.find(i => i.id === 'block:gym').notify, false);
  assert.equal(d2.items.find(i => i.id === 'block:gym').why, 'mine');
  // weekly placement appears only when placed
  const d3 = buildDay(decodeSnapshot(snap), '2026-09-24', content, {}, { pressureTest: { day: 4, time: '15:30' }, alarms: [{ day: '*', time: '12:00' }] });
  assert.ok(d3.items.find(i => i.id === 'weekly:pressure-test'));
  assert.ok(d3.items.find(i => i.id === 'weekly:belief-alarm-1'));
  assert.ok(!day.items.find(i => i.id === 'weekly:pressure-test'));
});
test("6 R's: four a day, seeded, never in class or practice, an hour apart", () => {
  const snap = JSON.parse(readFileSync(new URL('./fixtures/snapshot.json', import.meta.url)));
  const content = JSON.parse(readFileSync(new URL('../content/items.json', import.meta.url)));
  const parsed = decodeSnapshot(snap);
  // add a practice to Thursday so the avoidance is exercised
  parsed.obligations.push({ date: '2026-09-24', text: 'Practice 4:00–5:50' });
  const day = buildDay(parsed, '2026-09-24', content, {}, {});
  const six = day.items.filter(i => i.kind === 'sixr');
  assert.equal(six.length, 4);
  const busy = day.items.filter(i => i.kind === 'class' || i.kind === 'gameday');
  assert.equal(busy.length, 3);
  for (const s of six) {
    assert.ok(s.notify);
    for (const b of busy) assert.ok(s.start < b.start - 10 || s.start >= b.end + 10, `${s.startShort} inside ${b.title}`);
  }
  for (let i = 1; i < six.length; i++) assert.ok(six[i].start - six[i - 1].start >= 60);
  const again = buildDay(parsed, '2026-09-24', content, {}, {});
  assert.deepEqual(again.items.filter(i => i.kind === 'sixr').map(i => i.start), six.map(i => i.start));
  const other = buildDay(parsed, '2026-09-25', content, {}, {});
  assert.notDeepEqual(other.items.filter(i => i.kind === 'sixr').map(i => i.start), six.map(i => i.start));
  assert.equal(buildDay(parsed, '2026-09-24', content, {}, { sixr: { count: 0 } }).items.filter(i => i.kind === 'sixr').length, 0);
  // the in-depth sheet rides on the ping and on the morning meditate step
  assert.equal(six[0].guide.steps.length, 7);
  assert.equal(six[0].guide.steps[0].title, 'Recognize');
  assert.ok(six[0].guide.whole.length > 200);
  const med = day.items[0].steps.find(s => s.id === 'am:meditate-5-minutes');
  assert.equal(med.guide.steps.length, 7);
  assert.equal(day.items[0].steps.find(s => s.id === 'am:pray').guide, null);
  // an override can attach a guide inline
  const d4 = buildDay(parsed, '2026-09-24', content, { 'am:pray': { guide: { steps: [{ title: 'One', do: 'x', intention: 'y' }], whole: 'w' } } }, {});
  assert.equal(d4.items[0].steps.find(s => s.id === 'am:pray').guide.steps.length, 1);
});
test('done semantics', () => {
  const block = { id: 'b', steps: [{ id: 's1' }, { id: 's2' }] };
  assert.equal(isDone(block, null, { s1: 1, s2: 1 }), true);
  assert.equal(isDone(block, null, { s1: 1 }), false);
  assert.equal(isDone(block.steps[1], block, { b: 1 }), true);
});
function pick(t) { return [t.start, t.end]; }
