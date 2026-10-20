/**
 * Health view.
 *
 * Fetches GET /api/health (via apiFetch, so the backend resolves THIS account's own archive - see api/routes/health.py's module docstring)
 * and shows the API's own liveness report:
 * whether the caller's archive is attached and readable, whether a Telethon session exists, and the current archived message count.
 * See api/routes/health.py for exactly what each check does and doesn't cover
 * (notably: it does NOT confirm the userbot is currently connected to Telegram - that needs IPC, a Phase 3 addition per that file's own docstring).
 *
 * No polling - this is a manually-refreshed diagnostic view, not a live dashboard.
 * It re-fetches when the Refresh button is pressed and each time the tab is switched back to (app.js calls refreshHealthView()),
 * so the message count is current without a page reload.
 * Adding timed auto-refresh is a small change later if it turns out to be wanted, not something to build speculatively now.
 *
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t } from "../i18n.js";
import { apiFetch } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";
import { archiveFixLink } from "../lib/archive-error.js";
import { isUnchanged } from "../lib/refresh.js";

const healthViewState = {
  initialized: false,
  // The last report that was actually drawn ({ type: "data", data }), so a silent refresh can tell "nothing changed" and leave the page alone (see lib/refresh.js).
  lastRender: null,
  // Bumped by every loadHealth() call; a response is only drawn if it belongs to the newest call (a slow, older one must not overwrite a newer one).
  loadSeq: 0,
};

/**
 * Render the health check results as an HTML string.
 * @param {object} data - a HealthOut record from the API.
 * @returns {string}
 */
function renderHealthReport(data) {
  const statusLabel =
    data.status === "ok" ? t("health.statusOk") : t("health.statusDegraded");

  // A failed check is marked with the plain word "No" in a red badge; a passed check with a ✓ in a green one of the same shape.
  // History: this was a ✓/✗ pair that looked identical in colour and, for "...found", an ✗ that read like "found: yes" to people who had never connected anything;
  // it then became "No"-only, which left the passing rows with a wide empty gap that looked broken.
  // The two badges now differ in colour AND glyph/word, so neither mark is the only signal.
  // Both carry text a screen reader can say: "No" is real text, the ✓ has an aria-label of "Yes".
  const checkRow = (label, passed) => `
    <li class="health-check">
      <span class="health-check__mark">${
        passed
          ? `<span class="health-check__yes" role="img" aria-label="${t("health.yes")}">✓</span>`
          : `<span class="health-check__no">${t("health.no")}</span>`
      }</span>
      <span>${label}</span>
    </li>
  `;

  // session_exists: whether this account has signed in to Telegram (Settings -> Telegram setup).
  // Every account has its own session now, so this is a real per-account check - a plain ✗ here means "link Telegram", not "not applicable to you".
  const sessionRow = checkRow(t("health.sessionExists"), data.session_exists);

  // An account with no archive_db_ref at all hasn't FAILED a database check - there is simply nothing to check yet.
  // This row used to render an ✗ for it unconditionally, which read as "your database is broken" directly above a message saying it just isn't set up yet.
  // Same neutral "·" treatment the session row above uses for "not applicable".
  // "unavailable" and "misconfigured" ARE genuine failures and keep the ✗ - see HealthOut's own docstring for what tells them apart.
  const dbRow =
    data.archive_status === "unattached"
      ? `
    <li class="health-check health-check--info">
      <span class="health-check__mark" aria-hidden="true">·</span>
      <span>${t("health.dbNotSetUp")}</span>
    </li>
  `
      : checkRow(t("health.dbReadable"), data.db_readable);

  // archive_status distinguishes WHY the database check didn't pass (unattached vs. transiently unavailable vs. permanently misconfigured)
  // instead of collapsing all three into one flat "not readable" line - see HealthOut's own docstring.
  // "misconfigured" gets its own message rather than reusing "unavailable"'s "try again shortly" wording,
  // since retrying can never fix a database that was never created - this is deliberately an "administrator, now" message, not a "retry" one.
  // "unattached" is the only one of the three that's self-fixable BY THE USER (finish the setup wizard) -
  // same SELF_FIXABLE_REASONS reasoning as lib/archive-error.js (used by chats/messages/deleted/stats for the exact same three-way split),
  // so it's the only one that gets a link rather than just prose.
  // "unavailable" is a transient outage (nothing to click, just wait) and "misconfigured" is explicitly admin-only.
  // "ok" is the only case with an actual message count to show.
  const fix = archiveFixLink({ reason: "archive_unattached" });
  const settingsLink = ` <a href="${fix.href}" class="settings-fix-link">${fix.label}</a>`;
  const archiveMessage =
    data.archive_status === "unattached"
      ? `<p class="health-archive-message">${t("health.unattached")}${settingsLink}</p>`
      : data.archive_status === "unavailable"
        ? `<p class="health-archive-message">${t("health.unavailable")}</p>`
        : data.archive_status === "misconfigured"
          ? `<p class="health-archive-message health-archive-message--urgent">${t("health.misconfigured")}</p>`
          : `<p class="health-message-count">${t("health.messageCount")}: ${data.db_message_count}</p>`;

  return `
    <div class="health-status">
      <span class="info-badge">${statusLabel}</span>
    </div>
    <ul class="health-check-list">
      ${dbRow}
      ${sessionRow}
    </ul>
    ${archiveMessage}
    <button id="health-refresh" class="health-refresh-btn" type="button">${t("health.refresh")}</button>
  `;
}

/**
 * Fetch and render the current health report.
 *
 * @param {HTMLElement} root
 * @param {{ silent?: boolean }} [options] silent=true is the tab-switch refresh: it keeps the current report on screen while fetching
 *   (no "Loading…" flash), leaves the page untouched if nothing changed, and keeps the old report when the request merely fails
 *   (a network blip should not wipe a good report), exactly like the other views' silent refresh.
 */
async function loadHealth(root, { silent = false } = {}) {
  const seq = ++healthViewState.loadSeq;
  if (!silent) {
    root.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;
  }

  let data;
  try {
    // apiFetch (not plain fetch) - GET /api/health requires a token like every other route now that each account has its own,
    // potentially-nonexistent archive (see api/routes/health.py's module docstring):
    // there's no database to check without first knowing which caller is asking.
    const res = await apiFetch("/api/health");
    if (seq !== healthViewState.loadSeq) return; // a newer load started while this one was in flight
    // Not using res.ok for the normal case - health.py always returns 200, even when status is "degraded"
    // (that's the point: the body carries the real state, not the HTTP status - see its docstring).
    // A non-200 here means something is more seriously wrong, e.g. an expired session that apiFetch couldn't silently refresh
    // (rare on this view - see apiFetch's own docstring for when it redirects instead of returning here).
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      if (seq !== healthViewState.loadSeq) return;
      healthViewState.lastRender = null;
      root.innerHTML = `<div class="empty-state">${describeError(body.detail)}</div>`;
      return;
    }
    data = await res.json();
  } catch {
    if (seq !== healthViewState.loadSeq) return;
    if (silent && healthViewState.lastRender) return; // keep the last good report
    healthViewState.lastRender = null;
    root.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
    return;
  }
  if (seq !== healthViewState.loadSeq) return; // superseded while reading the body

  if (silent && isUnchanged(healthViewState.lastRender, data)) return;
  healthViewState.lastRender = { type: "data", data };
  root.innerHTML = renderHealthReport(data);
  root
    .querySelector("#health-refresh")
    .addEventListener("click", () => loadHealth(root));
}

/** Entry point called by app.js the first time the Health tab is opened. */
function initHealthView() {
  if (healthViewState.initialized) return;
  healthViewState.initialized = true;

  const root = document.getElementById("health-root");
  if (root) loadHealth(root);
}

/**
 * Called by app.js every time the Health tab is switched back to after its first open (the first open is initHealthView()'s job).
 * If the previous attempt ended in an error screen, this is also the retry: there is no report on screen to protect, so the fetch is allowed to replace it.
 */
function refreshHealthView() {
  if (!healthViewState.initialized) return;
  const root = document.getElementById("health-root");
  if (root) loadHealth(root, { silent: true });
}

export { initHealthView, refreshHealthView };

// Re-render on language change.
// Re-fetches (rather than caching like the other views) since this is a live diagnostic - the whole point of a health check is that it reflects the current moment,
// not the last time the tab happened to be opened.
document.addEventListener("televault:langchange", () => {
  if (!healthViewState.initialized) return;
  const root = document.getElementById("health-root");
  if (root) loadHealth(root);
});
