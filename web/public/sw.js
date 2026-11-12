const CACHE = "opsdeck-v2";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(["/", "/manifest.json"])));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const staticPath = url.pathname === "/" || url.pathname === "/manifest.json"
    || url.pathname.startsWith("/assets/") || /^\/icon(?:-\d+)?\.(?:png|svg)$/.test(url.pathname);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !staticPath) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then(async (response) => {
        if (response.ok) {
          try {
            const cache = await caches.open(CACHE);
            await cache.put(event.request, response.clone());
          } catch {
            // A full or disabled cache must not hide a successful response.
          }
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        if (event.request.mode === "navigate") return caches.match("/");
        return Response.error();
      }),
  );
});
