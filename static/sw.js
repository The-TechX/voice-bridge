self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {}

  const title = data.title || 'TCHX Voice';
  const options = {
    body: data.body || 'Nuevo mensaje de voz. Toca para escuchar.',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: data.message_id ? `voice-${data.message_id}` : 'voice-message',
    renotify: true,
    data: {
      url: data.url || '/',
      message_id: data.message_id || null,
    },
  };

  event.waitUntil((async () => {
    if ('setAppBadge' in self.navigator) {
      try { await self.navigator.setAppBadge(); } catch {}
    }
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || '/', self.location.origin).href;

  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      if ('navigate' in client) await client.navigate(targetUrl);
      if ('focus' in client) return client.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
  })());
});
