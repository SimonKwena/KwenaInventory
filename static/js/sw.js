self.addEventListener('push', function (event) {
    let data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = { title: 'Kwena Storage', body: event.data ? event.data.text() : '' };
    }
    const title = data.title || 'Kwena Storage';
    const body = data.body || '';
    const url = data.url || '/';
    event.waitUntil(
        self.registration.showNotification(title, {
            body: body,
            icon: '/static/images/icon-192.png',
            badge: '/static/images/badge-72.png',
            data: { url: url },
            requireInteraction: false,
        })
    );
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    const url = event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (windowClients) {
            for (let i = 0; i < windowClients.length; i++) {
                const client = windowClients[i];
                if (client.url === url && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(url);
            }
        })
    );
});

self.addEventListener('activate', function (event) {
    event.waitUntil(self.clients.claim());
});
