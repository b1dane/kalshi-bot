/* Kalshi BTC Bot — service worker */
var CACHE_NAME = "kalshi-bot-v2";
var STATIC_ASSETS = [
  "/",
  "/static/index.html",
  "/static/style.css",
  "/static/script.js",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png"
];

self.addEventListener("install", function(e) {
  e.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(STATIC_ASSETS);
    }).then(function() { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE_NAME; })
            .map(function(k) { return caches.delete(k); })
      );
    }).then(function() { return self.clients.claim(); })
  );
});

// Network-first for API calls, cache-first for static assets.
self.addEventListener("fetch", function(e) {
  var url = new URL(e.request.url);

  // API: network-first, fall back to cache (stale-while-revalidate style)
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request)
        .then(function(resp) {
          var clone = resp.clone();
          caches.open(CACHE_NAME).then(function(cache) {
            cache.put(e.request, clone);
          });
          return resp;
        })
        .catch(function() {
          return caches.match(e.request);
        })
    );
    return;
  }

  // Static: cache-first
  e.respondWith(
    caches.match(e.request).then(function(cached) {
      return cached || fetch(e.request).then(function(resp) {
        var clone = resp.clone();
        caches.open(CACHE_NAME).then(function(cache) { cache.put(e.request, clone); });
        return resp;
      });
    })
  );
});
