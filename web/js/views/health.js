/**
 * Health view.
 *
 * Fetches GET /api/health (via apiFetch, so the backend resolves THIS account's own archive - see api/routes/health.py's module docstring)
 * and shows the API's own liveness report:
 * whether the caller's archive is attached and readable, whether a Telethon session exists, and the current archived message count.
 * See api/routes/health.py for exactly what each check does and doesn't cover
 * (notably: it does NOT confirm the userbot is currently connected to Telegram — that needs IPC, a Phase 3 addition per that file's own docstring).
 *
 * No polling — this is a manually-refreshed diagnostic view, not a live dashboard.
 * Deliberately simple: one fetch, one refresh button.
 * Adding auto-refresh is a small change later if it turns out to be wanted, not something to build speculatively now.
 *
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t } from "../i18n.js";
import { apiFetch } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";

const healthViewState = {
  initialized: false,
};

/**
 * Render the health check results as an HTML string.
 * @param {object} data - a HealthOut record from the API.
 * @returns {string}
 */
function renderHealthReport(data) {
  const statusLabel =
    data.status === "ok" ? t("health.statusOk") : t("health.statusDegraded");

  const checkRow = (label, passed) => `
    <li class="health-check">
      <span class="health-check__mark" aria-hidden="true">${passed ? "✓" : "✗"}</span>
      <span>${label}</span>
    </li>
  `;

  // is_instance_owner: there is exactly one physical Telethon session per running instance, and it belongs to one account (see HealthOut's own docstring) -
  // showing a plain ✗ to every OTHER account would look like a persistent, personal problem rather than "not applicable to you",
  // so those accounts get an explanatory line instead of a checklist row here.
  const sessionRow = data.is_instance_owner
    ? checkRow(t("health.sessionExists"), data.session_exists)
    : `
    <li class="health-check health-check--info">
      <span class="health-check__mark" aria-hidden="true">·</span>
      <span>${t("health.sessionNotApplicable")}</span>
    </li>
  `;

  // archive_status distinguishes WHY the database check didn't pass (unattached vs. genuinely unavailable)
  // instead of collapsing both into one flat "not readable" line - see HealthOut's own docstring.
  // "ok" is the only case with an actual message count to show.
  const archiveMessage =
    data.archive_status === "unattached"
      ? `<p class="health-archive-message">${t("health.unattached")}</p>`
      : data.archive_status === "unavailable"
        ? `<p class="health-archive-message">${t("health.unavailable")}</p>`
        : `<p class="health-message-count">${t("health.messageCount")}: ${data.db_message_count}</p>`;

  return `
    <div class="health-status">
      <span class="info-badge">${statusLabel}</span>
    </div>
    <ul class="health-check-list">
      ${checkRow(t("health.dbReadable"), data.db_readable)}
      ${sessionRow}
    </ul>
    ${archiveMessage}
    <button id="health-refresh" class="health-refresh-btn" type="button">${t("health.refresh")}</button>
  `;
}

/** Fetch and render the current health report. */
async function loadHealth(root) {
  root.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;

  let data;
  try {
    // apiFetch (not plain fetch) - GET /api/health requires a token like every other route now that each account has its own,
    // potentially-nonexistent archive (see api/routes/health.py's module docstring):
    // there's no database to check without first knowing which caller is asking.
    const res = await apiFetch("/api/health");
    // Not using res.ok for the normal case — health.py always returns 200, even when status is "degraded"
    // (that's the point: the body carries the real state, not the HTTP status — see its docstring).
    // A non-200 here means something is more seriously wrong, e.g. an expired session that apiFetch couldn't silently refresh
    // (rare on this view - see apiFetch's own docstring for when it redirects instead of returning here).
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      root.innerHTML = `<div class="empty-state">${describeError(body.detail)}</div>`;
      return;
    }
    data = await res.json();
  } catch {
    root.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
    return;
  }

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

export { initHealthView };

// Re-render on language change.
// Re-fetches (rather than caching like the other views) since this is a live diagnostic — the whole point of a health check is that it reflects the current moment,
// not the last time the tab happened to be opened.
document.addEventListener("televault:langchange", () => {
  if (!healthViewState.initialized) return;
  const root = document.getElementById("health-root");
  if (root) loadHealth(root);
});
