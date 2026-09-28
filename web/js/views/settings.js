/**
 * Settings view.
 *
 * Two independent cards, matching the two separable concerns api/routes/telegram.py and api/routes/archive.py are deliberately kept as
 * (see both files' module docstrings - linking Telegram and having somewhere to store the archived messages don't imply each other at the API layer;
 * this view just happens to lay them out top-to-bottom):
 *
 *   1. "Connect Telegram" - drives the three-step handshake POST /telegram/credentials -> POST /telegram/link/send-code -> POST /telegram/link/confirm
 *      (which itself forks into a second confirm call if the account has 2FA enabled).
 *   2. "Archive database" - drives POST /archive/provision, gated on GET /api/health's own archive_status field rather than anything wizard-specific
 *      (see loadArchiveCard() below).
 *
 * STATE RESOLUTION:
 * on load, GET /telegram/status (has_credentials, has_session) decides which step the wizard opens on - see loadTelegramStatus() below - so a returning,
 * already-linked account lands on the "Linked" state instead of being sent back through credentials/phone/code every visit.
 * GET /auth/me's UserOut deliberately excludes the telegram_* columns (see its own docstring),
 * so this dedicated status endpoint exists specifically to answer "how far did I get?" without exposing the encrypted values themselves -
 * see TelegramStatusOut's docstring (api/schemas/telegram.py).
 *
 * The archive card resolves its own state the same way, via GET /api/health's archive_status field,
 * so "already provisioned" always renders correctly there regardless of which page load asks.
 *
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { apiFetch } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";

// Reasons from POST /telegram/link/confirm that mean "the handshake itself is dead" rather than "you typed the wrong thing" -
// these send the user back to the phone step (with an explanatory message) instead of just re-showing the code/password form for another try.
// See api/routes/telegram.py's confirm() docstring for what each one means server-side.
const HANDSHAKE_DEAD_REASONS = new Set([
  "telegram_no_pending_link",
  "telegram_too_many_attempts",
  "telegram_code_expired",
]);

const settingsViewState = {
  initialized: false,

  // "loading" | "credentials" | "phone" | "code" | "password" | "linked"
  // Starts on "loading" until GET /telegram/status resolves (see loadTelegramStatus) rather than guessing "credentials" up front -
  // a guess would flash-then-correct for anyone already linked.
  telegramStep: "loading",
  // Held only in memory, only to display "we sent a code to +1555..." on the code step - never sent anywhere except as the send-code request body itself.
  // Mirrors the backend's own choice not to persist it (see TelegramSendCodeIn's docstring in api/schemas/telegram.py).
  phone: "",
  telegramError: null,
  telegramBusy: false,

  // Last GET /api/health body, or null before the first fetch resolves.
  archiveStatus: null,
  archiveError: null,
  archiveBusy: false,
};

// ---------------------------------------------------------------------------
// Telegram linking card
// ---------------------------------------------------------------------------

function renderTelegramError() {
  if (!settingsViewState.telegramError) return "";
  return `<p class="settings-error" role="alert">${escapeHtml(settingsViewState.telegramError)}</p>`;
}

function renderCredentialsStep() {
  return `
    <p class="settings-help">${t("settings.credentialsHelp")}</p>
    <form id="settings-credentials-form">
      <label class="settings-field">
        <span>${t("settings.apiIdLabel")}</span>
        <input id="settings-api-id" name="api_id" type="text" inputmode="numeric" autocomplete="off" required />
      </label>
      <label class="settings-field">
        <span>${t("settings.apiHashLabel")}</span>
        <input id="settings-api-hash" name="api_hash" type="password" autocomplete="off" required />
      </label>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${settingsViewState.telegramBusy ? "disabled" : ""}>
        ${settingsViewState.telegramBusy ? t("settings.savingCredentials") : t("settings.credentialsSubmit")}
      </button>
    </form>
  `;
}

function renderPhoneStep() {
  return `
    <form id="settings-phone-form">
      <label class="settings-field">
        <span>${t("settings.phoneLabel")}</span>
        <input id="settings-phone" name="phone" type="tel" placeholder="${t("settings.phonePlaceholder")}" autocomplete="off" required />
      </label>
      <p class="settings-help">${t("settings.phoneHelp")}</p>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${settingsViewState.telegramBusy ? "disabled" : ""}>
        ${settingsViewState.telegramBusy ? t("settings.sendingCode") : t("settings.sendCodeSubmit")}
      </button>
      <button type="button" id="settings-back-to-credentials" class="modal__btn">
        ${t("settings.useDifferentCredentials")}
      </button>
    </form>
  `;
}

function renderCodeStep() {
  return `
    <form id="settings-code-form">
      <p class="settings-help">${t("settings.codeHelp").replace("{phone}", escapeHtml(settingsViewState.phone))}</p>
      <label class="settings-field">
        <span>${t("settings.codeLabel")}</span>
        <input id="settings-code" name="code" type="text" inputmode="numeric" autocomplete="off" required />
      </label>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${settingsViewState.telegramBusy ? "disabled" : ""}>
        ${settingsViewState.telegramBusy ? t("settings.verifying") : t("settings.codeSubmit")}
      </button>
      <button type="button" id="settings-start-over" class="modal__btn">${t("settings.startOver")}</button>
    </form>
  `;
}

function renderPasswordStep() {
  return `
    <form id="settings-password-form">
      <p class="settings-help">${t("settings.passwordHelp")}</p>
      <label class="settings-field">
        <span>${t("settings.passwordLabel")}</span>
        <input id="settings-password" name="password" type="password" autocomplete="off" required />
      </label>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${settingsViewState.telegramBusy ? "disabled" : ""}>
        ${settingsViewState.telegramBusy ? t("settings.verifying") : t("settings.passwordSubmit")}
      </button>
      <button type="button" id="settings-start-over" class="modal__btn">${t("settings.startOver")}</button>
    </form>
  `;
}

function renderLinkedStep() {
  return `
    <p><span class="patina-badge">${t("settings.linkedBadge")}</span></p>
    <p class="settings-help">${t("settings.linkedBody")}</p>
    <button type="button" id="settings-relink" class="modal__btn">${t("settings.relinkButton")}</button>
  `;
}

function renderLoadingStep() {
  return `<p class="settings-help">${t("settings.telegramStatusChecking")}</p>`;
}

function renderTelegramCard() {
  const stepRenderers = {
    loading: renderLoadingStep,
    credentials: renderCredentialsStep,
    phone: renderPhoneStep,
    code: renderCodeStep,
    password: renderPasswordStep,
    linked: renderLinkedStep,
  };
  const body = (
    stepRenderers[settingsViewState.telegramStep] || renderCredentialsStep
  )();
  return `
    <section class="settings-card">
      <h2>${t("settings.telegramTitle")}</h2>
      <p class="settings-help">${t("settings.telegramIntro")}</p>
      ${body}
    </section>
  `;
}

/** Reset to a clean, error-free step and re-render - shared by every "go back / start over" button. */
function goToTelegramStep(root, step, { error = null } = {}) {
  settingsViewState.telegramStep = step;
  settingsViewState.telegramError = error;
  settingsViewState.telegramBusy = false;
  renderRoot(root);
}

/**
 * POST a JSON body to an /api/telegram/... endpoint and return the parsed response, throwing the already-localized error message on failure.
 * Shared by all four form submit handlers below so each one only has to deal with its own success path.
 */
async function postTelegramStep(path, body) {
  const res = await apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const reason = data?.detail?.reason;
    throw { message: describeError(data.detail), reason };
  }
  return data;
}

function attachTelegramHandlers(root) {
  const credentialsForm = document.getElementById("settings-credentials-form");
  credentialsForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    settingsViewState.telegramBusy = true;
    settingsViewState.telegramError = null;
    renderRoot(root);
    try {
      await postTelegramStep("/api/telegram/credentials", {
        api_id: document.getElementById("settings-api-id").value.trim(),
        api_hash: document.getElementById("settings-api-hash").value.trim(),
      });
      goToTelegramStep(root, "phone");
    } catch (err) {
      goToTelegramStep(root, "credentials", {
        error: err.message || t("common.error"),
      });
    }
  });

  const phoneForm = document.getElementById("settings-phone-form");
  phoneForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const phone = document.getElementById("settings-phone").value.trim();
    settingsViewState.telegramBusy = true;
    settingsViewState.telegramError = null;
    renderRoot(root);
    try {
      await postTelegramStep("/api/telegram/link/send-code", { phone });
      settingsViewState.phone = phone;
      goToTelegramStep(root, "code");
    } catch (err) {
      goToTelegramStep(root, "phone", {
        error: err.message || t("common.error"),
      });
    }
  });
  document
    .getElementById("settings-back-to-credentials")
    ?.addEventListener("click", () => {
      goToTelegramStep(root, "credentials");
    });

  const codeForm = document.getElementById("settings-code-form");
  codeForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = document.getElementById("settings-code").value.trim();
    settingsViewState.telegramBusy = true;
    settingsViewState.telegramError = null;
    renderRoot(root);
    try {
      const result = await postTelegramStep("/api/telegram/link/confirm", {
        code,
      });
      goToTelegramStep(root, result.needs_password ? "password" : "linked");
    } catch (err) {
      if (HANDSHAKE_DEAD_REASONS.has(err.reason)) {
        // The pending link itself is gone (expired/exhausted) - re-sending a fresh code from the phone step is the only way forward,
        // so land there directly rather than re-showing a code box with nothing to confirm against.
        goToTelegramStep(root, "phone", { error: err.message });
      } else {
        goToTelegramStep(root, "code", {
          error: err.message || t("common.error"),
        });
      }
    }
  });

  const passwordForm = document.getElementById("settings-password-form");
  passwordForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = document.getElementById("settings-password").value;
    settingsViewState.telegramBusy = true;
    settingsViewState.telegramError = null;
    renderRoot(root);
    try {
      await postTelegramStep("/api/telegram/link/confirm", { password });
      goToTelegramStep(root, "linked");
    } catch (err) {
      if (HANDSHAKE_DEAD_REASONS.has(err.reason)) {
        goToTelegramStep(root, "phone", { error: err.message });
      } else {
        goToTelegramStep(root, "password", {
          error: err.message || t("common.error"),
        });
      }
    }
  });

  // Shared by the code and password steps - both mean "abandon this handshake, restart from the phone".
  // Only one of the two steps (and so only one #settings-start-over button) is ever in the DOM at once.
  document
    .getElementById("settings-start-over")
    ?.addEventListener("click", () => goToTelegramStep(root, "phone"));
  document.getElementById("settings-relink")?.addEventListener("click", () => {
    goToTelegramStep(root, "credentials");
  });
}

/**
 * Resolve the wizard's starting step from GET /telegram/status instead of always assuming "credentials".
 *
 * Falls back to "credentials" (the old universal default) on any failure to reach the endpoint at all - that's the safe direction to fail in,
 * since starting the wizard from scratch is always harmless (every step is re-doable, see this module's docstring),
 * whereas guessing "linked" when we're not sure would hide a real problem behind a reassuring badge.
 */
async function loadTelegramStatus(root) {
  try {
    const res = await apiFetch("/api/telegram/status");
    if (!res.ok) {
      settingsViewState.telegramStep = "credentials";
    } else {
      const status = await res.json();
      settingsViewState.telegramStep = status.has_session
        ? "linked"
        : status.has_credentials
          ? "phone"
          : "credentials";
    }
  } catch {
    settingsViewState.telegramStep = "credentials";
  }
  renderRoot(root);
}

// ---------------------------------------------------------------------------
// Archive database card
// ---------------------------------------------------------------------------

function renderArchiveCard() {
  const status = settingsViewState.archiveStatus;

  let body;
  if (settingsViewState.archiveBusy && !status) {
    body = `<p class="settings-help">${t("settings.archiveStatusChecking")}</p>`;
  } else if (status?.archive_status === "ok") {
    body = `<p><span class="patina-badge">${t("settings.archiveStatusReady").replace("{count}", String(status.db_message_count ?? 0))}</span></p>`;
  } else {
    const missingLabel =
      status?.archive_status === "unavailable"
        ? t("settings.archiveStatusUnavailable")
        : t("settings.archiveStatusMissing");
    body = `
      <p class="settings-help">${missingLabel}</p>
      <button type="button" id="settings-provision-btn" class="modal__btn modal__btn--primary" ${settingsViewState.archiveBusy ? "disabled" : ""}>
        ${settingsViewState.archiveBusy ? t("settings.provisioning") : t("settings.provisionButton")}
      </button>
    `;
  }

  const errorHtml = settingsViewState.archiveError
    ? `<p class="settings-error" role="alert">${escapeHtml(settingsViewState.archiveError)}</p>`
    : "";

  return `
    <section class="settings-card">
      <h2>${t("settings.archiveTitle")}</h2>
      <p class="settings-help">${t("settings.archiveIntro")}</p>
      ${body}
      ${errorHtml}
      <button type="button" id="settings-refresh-archive" class="settings-refresh-btn">${t("settings.refreshStatus")}</button>
    </section>
  `;
}

/**
 * Re-fetch GET /api/health and store just the fields this card cares about.
 *
 * preserveError=true skips clearing archiveError at the start - used only when a provision attempt just set one (see the click handler below):
 * without this, calling loadArchiveStatus() right after to pick up the fresh state would wipe that message before it ever painted,
 * since this function's own first step is normally "clear whatever error was showing before".
 */
async function loadArchiveStatus(root, { preserveError = false } = {}) {
  settingsViewState.archiveBusy = true;
  if (!preserveError) settingsViewState.archiveError = null;
  renderRoot(root);
  try {
    const res = await apiFetch("/api/health");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      settingsViewState.archiveError = describeError(data.detail);
    } else {
      settingsViewState.archiveStatus = await res.json();
      if (!preserveError) settingsViewState.archiveError = null;
    }
  } catch {
    settingsViewState.archiveError = t("common.error");
  }
  settingsViewState.archiveBusy = false;
  renderRoot(root);
}

function attachArchiveHandlers(root) {
  document
    .getElementById("settings-refresh-archive")
    ?.addEventListener("click", () => loadArchiveStatus(root));
  document
    .getElementById("settings-provision-btn")
    ?.addEventListener("click", async () => {
      settingsViewState.archiveBusy = true;
      settingsViewState.archiveError = null;
      renderRoot(root);

      let provisionFailed = false;
      try {
        const res = await apiFetch("/api/archive/provision", {
          method: "POST",
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // archive_already_provisioned here means someone else beat us to it
          // (e.g. a manual scripts/manage_admin.py set-archive run in the time since this card last loaded) -
          // not really a failure, so we still refresh the status underneath the message rather than leaving the stale "missing" view up alongside it.
          provisionFailed = true;
          settingsViewState.archiveError = describeError(data.detail);
        }
      } catch {
        provisionFailed = true;
        settingsViewState.archiveError = t("common.error");
      }
      await loadArchiveStatus(root, { preserveError: provisionFailed });
    });
}

// ---------------------------------------------------------------------------
// Root render / init
// ---------------------------------------------------------------------------

function renderRoot(root) {
  root.innerHTML = `${renderTelegramCard()}${renderArchiveCard()}`;
  attachTelegramHandlers(root);
  attachArchiveHandlers(root);
}

function initSettingsView() {
  const root = document.getElementById("settings-root");
  if (!root) return;
  if (settingsViewState.initialized) return;
  settingsViewState.initialized = true;

  renderRoot(root);
  loadTelegramStatus(root);
  loadArchiveStatus(root);
}

export { initSettingsView };

// Re-render in the new language - same pattern as the other views (e.g. health.js).
// No re-fetch: the archive status already held in memory is still accurate, only its rendered labels need to change.
document.addEventListener("televault:langchange", () => {
  if (!settingsViewState.initialized) return;
  const root = document.getElementById("settings-root");
  if (root) renderRoot(root);
});
