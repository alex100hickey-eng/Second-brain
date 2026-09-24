// The ONLY writes Intention makes into the training app's snapshot, and only into
// three table pages: BLUE LIST, SCOREBOARD - Confidence and Control, 50/50 -> Log.
// Protocol (from the brief): GET fresh -> back it up -> change only that table's
// rows -> PUT the whole snapshot with a new rev -> read back and verify.
import { fetchSnapshot, putSnapshot } from './snapshot.mjs';
import { setJSON, K } from './store.mjs';

const TABLES = {
  blue:       { title: 'BLUE LIST', cols: 3 },
  scoreboard: { title: 'SCOREBOARD - Confidence and Control', cols: 5 },
  fifty:      { title: 'Log', cat: '3', cols: 3 },
};

function locate(lib, spec) {
  for (const [c, cat] of Object.entries(lib)) {
    if (spec.cat && c !== spec.cat) continue;
    const pages = (cat && cat.pages) || [];
    const i = pages.findIndex(p => p && p.type === 'table' && p.title === spec.title);
    if (i >= 0) return { c, i, page: pages[i] };
  }
  return null;
}

/**
 * body: { table: 'blue'|'scoreboard'|'fifty', row: [..] }            -> fill the first empty row (or append)
 *       { table, rows: [[..],[..]] }                                -> several rows
 *       { table, match: {col: 0, value: 'W1'}, row: [..] }          -> replace the row whose column equals value, else fill/append
 */
export async function writeBack(body) {
  const spec = TABLES[body.table];
  if (!spec) throw new Error(`table must be one of ${Object.keys(TABLES).join(', ')}`);
  const rows = body.rows || (body.row ? [body.row] : null);
  if (!rows || !rows.every(r => Array.isArray(r))) throw new Error('row must be an array of cells');

  const { snap, fromCache } = await fetchSnapshot({ allowCache: false });
  if (fromCache) throw new Error('refusing to write from a cached snapshot');
  const keys = { ...snap.keys };
  const lib = JSON.parse(keys.workoutLibrary_v2);
  const loc = locate(lib, spec);
  if (!loc) throw new Error(`table "${spec.title}" not found in the library`);

  // back up the live snapshot before touching it
  await setJSON(K.backup(snap.rev), { savedAt: Date.now(), reason: `writeback:${body.table}`, snap });

  const table = loc.page;
  const width = (table.columns || []).length || spec.cols;
  const norm = (r) => { const o = r.slice(0, width).map(x => String(x ?? '')); while (o.length < width) o.push(''); return o; };
  const isEmpty = (r) => !r || r.every(x => !String(x ?? '').trim());
  table.rows = table.rows || [];
  const changed = [];
  for (const raw of rows) {
    const row = norm(raw);
    let idx = -1;
    if (body.match && Number.isInteger(body.match.col)) {
      idx = table.rows.findIndex(r => String(r[body.match.col] ?? '').trim() === String(body.match.value).trim());
    }
    if (idx < 0) idx = table.rows.findIndex(isEmpty);
    if (idx < 0) { table.rows.push(row); idx = table.rows.length - 1; } else table.rows[idx] = row;
    changed.push({ index: idx, row });
  }
  keys.workoutLibrary_v2 = JSON.stringify(lib);

  // never PUT from a stale copy: re-check the rev right before writing
  const again = await fetchSnapshot({ allowCache: false });
  if (again.snap.rev !== snap.rev) throw new Error(`snapshot changed under us (rev ${snap.rev} -> ${again.snap.rev}); try again`);
  const rev = await putSnapshot(keys);

  // read back and verify
  const check = await fetchSnapshot({ allowCache: false });
  if (check.snap.rev !== rev) throw new Error(`write not visible on read-back (expected rev ${rev}, saw ${check.snap.rev})`);
  const libBack = JSON.parse(check.snap.keys.workoutLibrary_v2);
  const back = locate(libBack, spec);
  for (const c of changed) {
    const got = back && back.page.rows[c.index];
    if (!got || got.join('\u0001') !== c.row.join('\u0001')) throw new Error(`read-back mismatch at row ${c.index}`);
  }
  return { ok: true, table: spec.title, rev, prevRev: snap.rev, changed, totalRows: table.rows.length };
}
