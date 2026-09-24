/* Intention service worker: shows pushes, opens the right item on tap, caches the shell. */
const VERSION = 'v10';
const SHELL = ['/', '/index.html', '/app.js?v=10', '/style.css?v=10', '/manifest.webmanifest', '/icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== VERSION && k !== NAV_CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

// Network first for everything; fall back to the cached shell when offline.
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api')) return;
  e.respondWith((async () => {
    try {
      const r = await fetch(e.request);
      if (r.ok && SHELL.includes(url.pathname + url.search)) { const c = await caches.open(VERSION); c.put(e.request, r.clone()); }
      return r;
    } catch {
      const hit = await caches.match(e.request) || (url.pathname === '/' ? await caches.match('/index.html') : null);
      return hit || Response.error();
    }
  })());
});

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: 'Intention', body: e.data ? e.data.text() : '' }; }
  const title = d.title || 'Intention';
  const opts = {
    body: d.body || '',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: { url: d.url || '/', id: d.id || null, date: d.date || null },
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    timestamp: Date.now(),
  };
  // iOS drops the subscription after a few pushes that show nothing, so always show one.
  e.waitUntil(Promise.all([self.registration.showNotification(title, opts), swlog('push', d.id || title)]));
});

// Where a tapped notification should take the app. iOS suspends the page in the background and can
// drop a postMessage, so the target is ALSO parked in cache storage; the page reads it on every
// load / return to the foreground (see consumePending in app.js).
const NAV_CACHE = 'intention-nav';
const putJSON = async (path, obj) => { const c = await caches.open(NAV_CACHE); await c.put(path, new Response(JSON.stringify(obj), { headers: { 'content-type': 'application/json' } })); };
async function setPending(url) { try { await putJSON('/__pending', { url, t: Date.now() }); } catch {} }
// The service worker cannot talk to the API (no key), so it leaves a log the page uploads at boot.
async function swlog(step, detail) {
  try {
    const c = await caches.open(NAV_CACHE);
    const r = await c.match('/__swlog');
    const arr = r ? await r.json().catch(() => []) : [];
    arr.push({ t: Date.now(), step, detail: String(detail || '').slice(0, 200) });
    while (arr.length > 30) arr.shift();
    await putJSON('/__swlog', arr);
  } catch {}
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  const work = (async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (wins.length) {
      const c = wins[0];
      let how = 'focus';
      // 1. navigate the open window if the browser allows it
      if (typeof c.navigate === 'function') { try { await c.navigate(target); how = 'navigate'; } catch (err) { how = 'navigate-failed:' + (err && err.message); } }
      // 2. tell the page (it also checks the parked target when it wakes up)
      try { c.postMessage({ type: 'open', url: target }); } catch {}
      if ('focus' in c) { try { await c.focus(); } catch {} }
      return `client(${wins.length}):${how}`;
    }
    // 3. app closed: open it on the item
    if (self.clients.openWindow) { try { const w = await self.clients.openWindow(target); return 'openWindow:' + (w ? 'ok' : 'null'); } catch (err) { return 'openWindow-failed:' + (err && err.message); } }
    return 'no-openWindow';
  })();
  e.waitUntil(Promise.all([
    setPending(target),
    work.then(how => swlog('click', how + ' ' + target), err => swlog('click-error', err && err.message)),
  ]));
});
