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
  telegram_not_linked: "error.telegramNotLinked",
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
  telegram_qr_not_scanned: "error.telegramQrNotScanned",

  // Telegram device management (api/routes/telegram_sessions.py) - "unreadable" reuses the "invalid" wording on purpose: both mean "link again".
  telegram_session_invalid: "error.telegramSessionInvalid",
  telegram_session_unreadable: "error.telegramSessionUnreadable",
  telegram_session_too_new: "error.telegramSessionTooNew",
  telegram_device_not_found: "error.telegramDeviceNotFound",
  telegram_cannot_end_current: "error.telegramCannotEndCurrent",
  telegram_unreachable: "error.telegramUnreachable",

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
  // FastAPI's own request-validation failures (HTTP 422) put a LIST of per-field problems under `detail`, not a {reason} object.
  // Their text is English and technical, so show one generic localized hint rather than the raw list.
  if (Array.isArray(detail)) return t("error.invalidInput");
  return t("common.error");
}

/**
 * describeError() for actions that connect to Telegram AS the account (device list, ending devices, unlinking).
 *
 * The one difference: "already_running" is the archiver's own reason code and its generic text ("It's already running.") makes sense for the Start button only.
 * Here the same code means "the archiver holds this Telegram session right now", so say that instead.
 *
 * @param {unknown} detail - the parsed response body's `detail` field.
 */
function describeSessionError(detail) {
  if (
    detail &&
    typeof detail === "object" &&
    detail.reason === "already_running"
  ) {
    return t("error.stopArchiverFirst");
  }
  return describeError(detail);
}

export { describeError, describeSessionError };
