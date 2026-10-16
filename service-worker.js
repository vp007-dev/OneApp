const CACHE_NAME = "tractorcare-v2";
const ASSETS_TO_CACHE = [
  "/",
  "/static/pwa/manifest.json",
  "/static/pwa/icon-192.png",
  "/static/pwa/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE).catch(() => {});
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Network first, falling back to cache
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        return response;
      })
      .catch(() => {
        return caches.match(event.request);
      })
  );
});

self.addEventListener("push", function (event) {
  if (!event.data) return;
  try {
    const data = event.data.json();
    const title = data.notification?.title || data.title || "TractorCare Alert";
    const options = {
      body: data.notification?.body || data.body || "Policy update alert.",
      icon: "/static/pwa/icon-192.png",
      badge: "/static/pwa/icon-192.png",
      vibrate: [100, 50, 100],
      data: { url: "/" },
    };
    event.waitUntil(self.registration.showNotification(title, options));
  } catch (err) {
    console.error("Push notification handling error:", err);
  }
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window" }).then((clientList) => {
      for (const client of clientList) {
        if (client.url === "/" && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow("/");
    })
  );
});