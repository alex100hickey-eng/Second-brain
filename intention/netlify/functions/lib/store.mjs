// Intention's own state lives here (Netlify Blobs), never in the training app's
// sync node — the training app PUTs only its own keys and would wipe anything else.
import { getStore } from '@netlify/blobs';

const NAME = 'intention';
export const store = () => getStore({ name: NAME, consistency: 'strong' });

export async function getJSON(key, fallback) {
  const v = await store().get(key, { type: 'json' });
  return v == null ? fallback : v;
}
export async function setJSON(key, value) {
  await store().setJSON(key, value);
}
export async function del(key) {
  await store().delete(key);
}
export async function listKeys(prefix) {
  const { blobs } = await store().list({ prefix });
  return blobs.map(b => b.key);
}

// Keys
export const K = {
  subs: 'subs',                       // { [id]: { subscription, ua, addedAt } }
  overrides: 'overrides',             // { [itemId]: { goal, how, why, tool, notify, lead, duration, title } }
  settings: 'settings',               // weekly placements + prefs
  oneoff: 'oneoff',                   // [ { id, at, title, body, url } ] ad-hoc / test pushes
  snapshotCache: 'snapshot-cache',    // { fetchedAt, snap }
  done: (date) => `done/${date}`,     // { [itemId]: epochMs }
  sent: (date) => `sent/${date}`,     // { [itemId]: epochMs }
  journal: (date) => `journal/${date}`,
  backup: (rev) => `backups/${rev}`,  // training snapshot before a write-back
  log: 'tick-log',                    // last 60 tick results, for debugging
  pairs: 'pairs',                     // { CODE: { createdAt } } single-use phone pairing codes
  diag: 'diag',                       // client diagnostics (enable-notifications steps), newest first
  lastPush: 'last-push',              // { id, date, url, title, at } — what the scheduler pinged most recently
};
