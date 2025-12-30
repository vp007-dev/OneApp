self.addEventListener("install", (event) => {
    console.log("Service Worker installed");
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    console.log("Service Worker activated");
});

self.addEventListener("fetch", () => {
    // For now we don't cache anything, just pass-through
});

self.addEventListener("push", function(event) {
  const data = event.data.json();
  self.registration.showNotification(data.notification.title, {
    body: data.notification.body,
    icon: "/static/pwa/icon-192.png"
  });
});