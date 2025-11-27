self.addEventListener("install", (e) => {
    console.log("Service Worker Installed");
});

self.addEventListener("fetch", (event) => {
    // pass-through for now
});
