/**
 * Minimal service worker: caches the static app shell only.
 *
 * Deliberately does NOT cache /api/* responses.
 * This is a personal archive of private message data - serving stale or cached API responses
 * (especially across the deleted-messages and search endpoints) would be actively misleading, not just stale.
 * Only the shell (HTML/CSS/JS) is cached, so the app *loads* offline;
 * it still needs a live connection to the API to show real data.
 *
 * Bump CACHE_NAME whenever shell files change, so old caches are evicted on the next visit instead of silently serving outdated JS/CSS.
 * This is NOT automatic and NOT enforced by anything -
 * a shell file changing without this being bumped in the SAME commit is exactly what happened across several earlier commits
 * (errors.js, i18n/{en,uk}.js, health.js, app.js, base.css all changed while this stayed at v18):
 * the fetch handler below is strict cache-first with no revalidation,
 * and the browser only re-runs `install` (which is what actually re-fetches SHELL_FILES) when it notices sw.js ITSELF changed byte-for-byte -
 * so a shell file edit with no accompanying CACHE_NAME bump can sit silently stale through any number of ordinary page reloads,
 * only fixable once a future commit happens to touch this file for an unrelated reason.
 * v19 fixes that specific staleness (bumped here for that reason alone, nothing else in this file's logic changed)
 * and also adds every shell file that had been missing from SHELL_FILES entirely
 * (login.html/register.html/telegram-setup.html and their own JS, lib/auth.js, lib/chat-filter.js, lib/archive-error.js) -
 * those were never stale, just never precached/offline-capable at all, which is a smaller gap but the same kind of drift.
 * v20 bumps again for the same reason as v19 (shell files changed: index.html, telegram-setup.html, app.js, auth.js, telegram-setup.js, base.css, i18n/{en,uk}.js)
 * and adds the new admin.js to SHELL_FILES.
 * v21 bumps for the same reason (index.html, app.js, base.css, i18n/{en,uk}.js, archiver-toggle.js, backfill.js, telegram-setup.js changed),
 * adds the new lib/dialog.js, and REMOVES admin.js from SHELL_FILES again: it is now only loaded (dynamic import) for the admin account,
 * so precaching it on every device would put the admin UI's code into every user's browser cache for no benefit.
 * Not precached means the fetch handler below simply falls through to the network for it - the admin panel needs the live API anyway.
 */

const CACHE_NAME = "televault-shell-v21";
const SHELL_FILES = [
  "/",
  "/index.html",
  "/login.html",
  "/register.html",
  "/telegram-setup.html",
  "/favicon.ico",
  "/css/variables.css",
  "/css/base.css",
  "/js/theme.js",
  "/js/i18n.js",
  "/js/lib/dom.js",
  "/js/lib/auth.js",
  "/js/lib/pagination.js",
  "/js/lib/order-toggle.js",
  "/js/lib/errors.js",
  "/js/lib/dialog.js",
  "/js/lib/chat-filter.js",
  "/js/lib/archive-error.js",
  "/js/archiver-toggle.js",
  "/js/app.js",
  "/js/views/chats.js",
  "/js/views/messages.js",
  "/js/views/deleted.js",
  "/js/views/stats.js",
  "/js/views/health.js",
  "/js/views/backfill.js",
  "/js/views/settings.js",
  "/js/views/telegram-setup.js",
  "/js/views/login.js",
  "/js/views/register.js",
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

  // Never intercept API calls - always go to the network.
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
