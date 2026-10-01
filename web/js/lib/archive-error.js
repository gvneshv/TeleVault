/**
 * Shared "render this view's archive-connection error" helper for chats.js/messages.js/deleted.js/stats.js -
 * the four views that all resolve the SAME per-account archive via api.dependencies.get_archive_connection()
 * (GET /api/chats, /messages, /deleted, /stats), and so can all fail with exactly the same three reasons
 * (archive_unattached / archive_unavailable / archive_misconfigured - see HealthOut's own docstring, api/schemas/common.py, for what distinguishes them) -
 * not because of anything specific to what each view itself does.
 * Centralizing this once means the "link to Settings" behavior below only has to be written (and kept correct) in one place,
 * not four times with four chances to drift apart.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "./dom.js";
import { describeError } from "./errors.js";

// Only reasons a signed-in, non-admin USER can actually fix themselves, by finishing the Telegram/archive setup wizard (web/telegram-setup.html) -
// archive_unavailable is a transient outage (nothing to click, just wait and retry) and archive_misconfigured is explicitly admin-only
// (the archive_db_ref points at a database that was never actually created - see db.is_missing_database_error()'s own docstring);
// sending the user to telegram-setup.html for either of those would land them somewhere with no button that could possibly help,
// which is worse than showing no link at all.
const SELF_FIXABLE_REASONS = new Set(["archive_unattached"]);

/**
 * Render an archive-connection error into `root`.
 * Reactively re-renderable on a later language change - each caller stores the SAME `detail` value it passed in here (see e.g. chats.js's own chatsViewState.lastRender)
 * and just calls this again on "televault:langchange",
 * rather than this module owning that state itself - every one of the four views already tracks its own last-rendered state (lastData, for the success case)
 * the same way, so this follows suit instead of introducing a second, different pattern next to it.
 *
 * @param {HTMLElement} root
 * @param {unknown} detail - the parsed error response body's `detail` field (see describeError()'s own docstring).
 */
function renderArchiveErrorState(root, detail) {
  const reason = detail && typeof detail === "object" ? detail.reason : null;
  const link = SELF_FIXABLE_REASONS.has(reason)
    ? ` <a href="/telegram-setup.html" class="settings-fix-link">${escapeHtml(t("common.goToSettings"))}</a>`
    : "";
  root.innerHTML = `<div class="empty-state">${escapeHtml(describeError(detail))}${link}</div>`;
}

export { renderArchiveErrorState };
