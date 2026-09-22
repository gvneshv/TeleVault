/**
 * Translates structured backend error bodies into localized text.
 *
 * FastAPI's HTTPException(status, {"message": ..., "reason": ...})
 * (see api/routes/telethon.py, api/routes/backfill.py, and api/dependencies.py) puts that dict under the response body's "detail" key.
 * The backend's "message" text is always English, so the UI maps known "reason" codes to a translated string instead of showing it directly
 * - falling back to the raw message only for reasons this UI doesn't specifically recognize.
 */

import { t } from "../i18n.js";

const ERROR_REASON_KEYS = {
  already_running: "error.alreadyRunning",
  not_running: "error.notRunning",
  archiver_connected: "error.archiverConnected",
  backfill_running: "error.backfillRunning",
  archive_unattached: "error.archiveUnattached",
  archive_unavailable: "error.archiveUnavailable",
  not_instance_owner: "error.notInstanceOwner",
  db_unavailable: "error.dbUnavailable",
  control_db_unavailable: "error.dbUnavailable",
};

/** @param {unknown} detail - the parsed response body's `detail` field. */
function describeError(detail) {
  if (detail && typeof detail === "object" && detail.reason) {
    const key = ERROR_REASON_KEYS[detail.reason];
    if (key) return t(key);
    return detail.message || t("common.error");
  }
  if (typeof detail === "string") return detail;
  return t("common.error");
}

export { describeError };
