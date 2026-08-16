/* sw.js — offline shell for RaanuBot.
 *
 * Deliberately does NOT cache /api/** at all.
 *
 * The obvious design is network-first with a cached fallback, so the app still
 * shows something when offline. For a trading dashboard that is the wrong
 * trade-off: a stale portfolio value or a stale "AUTO ON" pill is worse than a
 * blank one, because it looks current and invites a decision. API requests
 * therefore go straight to the network and are allowed to fail, and the page
 * renders its own empty state.
 *
 * What IS cached is the shell — the single HTML file, the manifest and the
 * icons — so the app opens instantly and works as an installed app rather than
 * a browser tab that needs a connection to show anything at all.
 */
const SHELL = 'raanu-shell-v6';   // v6: sticky notifications
const SHELL_URLS = [
  // "/" is deliberately NOT precached. It is the one file that changes on every
  // deploy, and precaching it meant a device could sit on an old copy — the app
  // showed the previous layout while the server had shipped a new one. It is
  // fetched network-first below and cached on each success instead.
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
  '/icons/mark.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL)
      .then(c => c.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())          // a new shell should not wait a tab close
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.clients.matchAll({type: 'window'}))
      .then(cs => cs.forEach(c => c.postMessage({type: 'sw-updated'})))
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Never touch other origins, non-GET, or the API.
  if (url.origin !== self.location.origin || req.method !== 'GET') return;
  if (url.pathname.startsWith('/api/')) return;

  // Navigations: network first so a deployed change is picked up, cached shell
  // only when the network genuinely fails.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(SHELL).then(c => c.put('/', copy));
          return res;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // Static assets: cache first, they are versioned by the cache name.
  event.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(req, copy));
      }
      return res;
    }))
  );
});


/* ── Web Push ──────────────────────────────────────────────────────────────
   The service worker receives pushes even when the app is closed — that is the
   whole point of routing them through here rather than the page. */
self.addEventListener('push', event => {
  let d = {title: 'RaanuBot', body: '', tag: 'raanu', url: '/'};
  try { d = Object.assign(d, event.data ? event.data.json() : {}); } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(d.title, {
      body: d.body,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      // Same tag replaces rather than stacks: three exits should not leave
      // three separate notifications for the same symbol.
      tag: d.tag,
      data: {url: d.url},
      vibrate: [80, 40, 80],
      // Stays on screen until dismissed by hand rather than fading after a few
      // seconds. A trade signal glanced at and lost is a signal not read.
      requireInteraction: d.requireInteraction === true,
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  // Focus an open window if there is one, rather than opening a second copy.
  event.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin) && 'focus' in c) return c.focus();
      }
      return clients.openWindow(url);
    })
  );
});
