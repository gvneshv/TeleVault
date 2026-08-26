/**
 * Minimal service worker: caches the static app shell only.
 *
 * Deliberately does NOT cache /api/* responses.
 * This is a personal archive of private message data — serving stale or cached API responses
 * (especially across the deleted-messages and search endpoints) would be actively misleading, not just stale.
 * Only the shell (HTML/CSS/JS) is cached, so the app *loads* offline;
 * it still needs a live connection to the API to show real data.
 *
 * Bump CACHE_NAME whenever shell files change, so old caches are evicted on the next visit instead of silently serving outdated JS/CSS.
 */

const CACHE_NAME = "televault-shell-v16";
const SHELL_FILES = [
  "/",
  "/index.html",
  "/favicon.ico",
  "/css/variables.css",
  "/css/base.css",
  "/js/theme.js",
  "/js/i18n.js",
  "/js/lib/dom.js",
  "/js/lib/pagination.js",
  "/js/lib/order-toggle.js",
  "/js/archiver-toggle.js",
  "/js/app.js",
  "/js/views/chats.js",
  "/js/views/messages.js",
  "/js/views/deleted.js",
  "/js/views/stats.js",
  "/js/views/health.js",
  "/js/views/backfill.js",
  "/js/i18n/en.js",
  "/js/i18n/uk.js",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // cache.addAll() is all-or-nothing: a single 404 among SHELL_FILES fails the *entire* install and leaves this SW with no cache at all, silently.
      // Cache each file independently instead, so one missing or renamed file (easy to hit mid-development) can't take the rest down.
      Promise.allSettled(
        SHELL_FILES.map((file) =>
          cache.add(file).catch((err) => {
            console.warn(`[sw] failed to precache ${file}:`, err);
          }),
        ),
      ),
    ),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
      ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never intercept API calls — always go to the network.
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  event.respondWith(
    caches
      .match(event.request)
      .then((cached) => cached || fetch(event.request))
      .catch((err) => {
        console.warn(`[sw] fetch failed for ${event.request.url}:`, err);
        throw err;
      }),
  );
});
