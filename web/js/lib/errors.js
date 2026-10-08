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
  archive_misconfigured: "error.archiveMisconfigured",
  not_instance_owner: "error.notInstanceOwner",
  not_admin: "error.notAdmin",
  db_unavailable: "error.dbUnavailable",
  control_db_unavailable: "error.dbUnavailable",

  // Sign-in / registration (api/routes/auth.py)
  invalid_credentials: "error.invalidCredentials",
  login_rate_limited_ip: "error.loginRateLimitedIp",
  login_rate_limited_account: "error.loginRateLimitedAccount",
  account_locked: "error.accountLocked",
  admin_exists: "error.adminExists",
  username_taken: "error.usernameTaken",
  invite_invalid: "error.inviteInvalid",
  terms_not_accepted: "error.termsNotAccepted",

  // Telegram linking flow (api/routes/telegram.py) -
  // previously left unwired deliberately (see that file's CHANGELOG entry) until the Settings view gave these reasons somewhere to actually be shown.
  invalid_api_credentials: "error.invalidApiCredentials",
  telegram_credentials_missing: "error.telegramCredentialsMissing",
  telegram_invalid_phone: "error.telegramInvalidPhone",
  telegram_flood_wait: "error.telegramFloodWait",
  telegram_no_pending_link: "error.telegramNoPendingLink",
  telegram_too_many_attempts: "error.telegramTooManyAttempts",
  telegram_password_required: "error.telegramPasswordRequired",
  telegram_code_required: "error.telegramCodeRequired",
  telegram_invalid_password: "error.telegramInvalidPassword",
  telegram_invalid_code: "error.telegramInvalidCode",
  telegram_code_expired: "error.telegramCodeExpired",

  // Archive provisioning (api/routes/archive.py, db/provisioning.py)
  archive_already_provisioned: "error.archiveAlreadyProvisioned",
  provisioning_permission_denied: "error.provisioningPermissionDenied",
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
