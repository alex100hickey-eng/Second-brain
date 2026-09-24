// Read the training app's live snapshot. The URL is a capability (works like a
// password) so it lives only in the TRAINING_SYNC_URL env var.
import { getJSON, setJSON, K } from './store.mjs';

export async function fetchSnapshot({ allowCache = true } = {}) {
  const url = process.env.TRAINING_SYNC_URL;
  if (!url) throw new Error('TRAINING_SYNC_URL not set');
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 8000);
    const r = await fetch(url, { signal: ctl.signal, cache: 'no-store' });
    clearTimeout(t);
    if (!r.ok) throw new Error(`training-sync HTTP ${r.status}`);
    const snap = await r.json();
    if (!snap || !snap.keys) throw new Error('training-sync returned no snapshot');
    setJSON(K.snapshotCache, { fetchedAt: Date.now(), snap }).catch(() => {});
    return { snap, fromCache: false };
  } catch (e) {
    if (!allowCache) throw e;
    const c = await getJSON(K.snapshotCache, null);
    if (c && c.snap) return { snap: c.snap, fromCache: true, cacheAge: Date.now() - c.fetchedAt, error: String(e) };
    throw e;
  }
}

/** Write the whole snapshot back with a new rev. Caller has already changed the
 *  keys it wanted; this just mints the rev and PUTs. Returns the new rev. */
export async function putSnapshot(keys) {
  const url = process.env.TRAINING_SYNC_URL;
  const rev = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  const body = JSON.stringify({ rev, keys });
  const r = await fetch(url, { method: 'PUT', body, headers: { 'Content-Type': 'text/plain;charset=UTF-8' } });
  if (!r.ok) throw new Error(`training-sync PUT HTTP ${r.status}: ${(await r.text().catch(() => '')).slice(0, 200)}`);
  return rev;
}
