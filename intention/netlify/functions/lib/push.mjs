// Web push (VAPID). Keys come from env; subscriptions from the store.
import webpush from 'web-push';
import { getJSON, setJSON, K } from './store.mjs';

let configured = false;
export function configure() {
  if (configured) return;
  const { VAPID_SUBJECT, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY } = process.env;
  if (!VAPID_PUBLIC_KEY || !VAPID_PRIVATE_KEY) throw new Error('VAPID keys not set');
  webpush.setVapidDetails(VAPID_SUBJECT || 'mailto:alex100hickey@gmail.com', VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY);
  configured = true;
}

export function subId(subscription) {
  // stable id per endpoint, short enough to be a key
  let h = 0; const s = subscription.endpoint || '';
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h.toString(36) + '-' + s.length;
}

/** Send one payload to every subscription. Dead subscriptions (404/410) are removed. */
export async function sendToAll(payload, subs) {
  configure();
  const all = subs || await getJSON(K.subs, {});
  const ids = Object.keys(all);
  if (!ids.length) return { sent: 0, subs: 0, removed: 0 };
  const body = JSON.stringify(payload);
  let sent = 0, removed = 0; let changed = false;
  const errors = [];
  await Promise.all(ids.map(async (id) => {
    const rec = all[id];
    try {
      await webpush.sendNotification(rec.subscription, body, { TTL: 120, urgency: 'high' });
      sent++;
    } catch (e) {
      const code = e && e.statusCode;
      if (code === 404 || code === 410) { delete all[id]; removed++; changed = true; }
      else errors.push(`${id}: ${code || ''} ${e && e.body ? String(e.body).slice(0, 120) : (e && e.message)}`);
    }
  }));
  if (changed) await setJSON(K.subs, all);
  return { sent, subs: ids.length, removed, errors };
}
