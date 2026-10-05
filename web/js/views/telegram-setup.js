/**
 * telegram-setup.html - a standalone page, not a tab inside index.html's app shell
 * (same reasoning as login.html/register.html - see lib/auth.js's module docstring for why those are separate pages rather than one page showing/hiding sections).
 *
 * Two independent cards, matching the two separable concerns api/routes/telegram.py and api/routes/archive.py are deliberately kept as
 * (see both files' module docstrings - linking Telegram and having somewhere to store the archived messages don't imply each other at the API layer;
 * this page just happens to lay them out top-to-bottom, plus a completion banner once both are actually done):
 *
 *   1. "Connect Telegram" - drives the three-step handshake POST /telegram/credentials -> POST /telegram/link/send-code -> POST /telegram/link/confirm
 *      (which itself forks into a second confirm call if the account has 2FA enabled).
 *   2. "Archive database" - drives POST /archive/provision, gated on GET /api/health's own archive_status field rather than anything wizard-specific
 *      (see loadArchiveCard() below).
 *
 * This used to be the content of the in-SPA Settings tab directly.
 * Moved out to its own page because Settings is planned to grow into a general account-settings page (change password, and whatever else comes later) -
 * the Telegram/archive wizard is a big, mostly one-time, first-run-shaped piece of UI that doesn't belong crowding that page permanently.
 * web/js/views/settings.js now just links here.
 *
 * Reached three ways: a fresh registration is redirected here directly (register.js) rather than to /index.html,
 * since a brand-new account can't do anything useful until this is done;
 * the Settings tab links here for anyone who wants to relink or check status later;
 * and a CLI-created account (scripts/manage_admin.py create/promote, which never goes through /auth/register)
 * also lands here the same way if they follow that same Settings link - this page doesn't distinguish how the account was created,
 * it only asks the API what's already been done (see loadTelegramStatus() below).
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
 */

import { t } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { apiFetch, isAdmin, fetchCurrentUser } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";

// Reasons from POST /telegram/link/confirm that mean "the handshake itself is dead" rather than "you typed the wrong thing" -
// these send the user back to the phone step (with an explanatory message) instead of just re-showing the code/password form for another try.
// See api/routes/telegram.py's confirm() docstring for what each one means server-side.
const HANDSHAKE_DEAD_REASONS = new Set([
  "telegram_no_pending_link",
  "telegram_too_many_attempts",
  "telegram_code_expired",
]);

const setupState = {
  // "loading" | "credentials" | "phone" | "code" | "password" | "linked"
  // Starts on "loading" until GET /telegram/status resolves (see loadTelegramStatus) rather than guessing "credentials" up front -
  // a guess would flash-then-correct for anyone already linked.
  telegramStep: "loading",
  // Held only in memory, only to display "we sent a code to +1555..." on the code step - never sent anywhere except as the send-code request body itself.
  // Mirrors the backend's own choice not to persist it (see TelegramSendCodeIn's docstring in api/schemas/telegram.py).
  phone: "",
  telegramError: null,
  telegramBusy: false,

  // Last GET /api/health body, or null before the first fetch resolves (or if it failed).
  archiveStatus: null,
  archiveError: null,
  // archiveBusy = "re-fetching status"; archiveProvisioning = "POST /archive/provision in flight".
  // Kept separate: they used to share one flag, so a plain Refresh flipped the Create button's label to "Creating…" even though nothing was being created.
  archiveBusy: false,
  archiveProvisioning: false,
};

// ---------------------------------------------------------------------------
// Telegram linking card
// ---------------------------------------------------------------------------

function renderTelegramError() {
  if (!setupState.telegramError) return "";
  return `<p class="settings-error" role="alert">${escapeHtml(setupState.telegramError)}</p>`;
}

const MY_TELEGRAM_URL = "https://my.telegram.org";
const MY_TELEGRAM_APPS_URL = "https://my.telegram.org/apps";

/**
 * An external link that opens in a NEW tab, so the person doesn't lose this page (and the half-filled wizard on it) while they go get their credentials.
 * rel="noopener noreferrer" is not optional with target="_blank": without it the opened page gets a handle back to this window via window.opener.
 * Link text is never user-supplied, so it's interpolated into the string as-is (no escapeHtml needed).
 */
function externalLink(href, text) {
  return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
}

/**
 * The "how do I even get an API ID/hash" walkthrough shown above the credentials form.
 * The link labels ("my.telegram.org", "API development tools") are fixed English on purpose: they name things on Telegram's own page,
 * which the person has to match against what they see there.
 */
function renderCredentialsGuide() {
  const siteLink = externalLink(MY_TELEGRAM_URL, "my.telegram.org");
  const appsLink = externalLink(MY_TELEGRAM_APPS_URL, "API development tools");
  const steps = [
    t("tgSetup.guideStep1").replace("{siteLink}", siteLink),
    t("tgSetup.guideStep2").replace("{appsLink}", appsLink),
    t("tgSetup.guideStep3"),
    t("tgSetup.guideStep4"),
    t("tgSetup.guideStep5"),
  ];
  return `
    <p class="settings-help">${t("tgSetup.guideIntro")}</p>
    <ol class="settings-guide">${steps.map((step) => `<li>${step}</li>`).join("")}</ol>
  `;
}

function renderCredentialsStep() {
  return `
    ${renderCredentialsGuide()}
    <form id="settings-credentials-form">
      <label class="settings-field">
        <span>${t("tgSetup.apiIdLabel")}</span>
        <input id="settings-api-id" name="api_id" type="text" inputmode="numeric" autocomplete="off" required />
      </label>
      <label class="settings-field">
        <span>${t("tgSetup.apiHashLabel")}</span>
        <input id="settings-api-hash" name="api_hash" type="password" autocomplete="off" required />
      </label>
      <p class="settings-help">${t("tgSetup.credentialsStoredNote")}</p>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${setupState.telegramBusy ? "disabled" : ""}>
        ${setupState.telegramBusy ? t("tgSetup.savingCredentials") : t("tgSetup.credentialsSubmit")}
      </button>
    </form>
  `;
}

function renderPhoneStep() {
  return `
    <form id="settings-phone-form">
      <label class="settings-field">
        <span>${t("tgSetup.phoneLabel")}</span>
        <input id="settings-phone" name="phone" type="tel" placeholder="${t("tgSetup.phonePlaceholder")}" autocomplete="off" required />
      </label>
      <p class="settings-help">${t("tgSetup.phoneHelp")}</p>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${setupState.telegramBusy ? "disabled" : ""}>
        ${setupState.telegramBusy ? t("tgSetup.sendingCode") : t("tgSetup.sendCodeSubmit")}
      </button>
      <button type="button" id="settings-back-to-credentials" class="modal__btn">
        ${t("tgSetup.useDifferentCredentials")}
      </button>
    </form>
  `;
}

function renderCodeStep() {
  return `
    <form id="settings-code-form">
      <p class="settings-help">${t("tgSetup.codeHelp").replace("{phone}", escapeHtml(setupState.phone))}</p>
      <label class="settings-field">
        <span>${t("tgSetup.codeLabel")}</span>
        <input id="settings-code" name="code" type="text" inputmode="numeric" autocomplete="off" required />
      </label>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${setupState.telegramBusy ? "disabled" : ""}>
        ${setupState.telegramBusy ? t("tgSetup.verifying") : t("tgSetup.codeSubmit")}
      </button>
      <button type="button" id="settings-start-over" class="modal__btn">${t("tgSetup.startOver")}</button>
    </form>
  `;
}

function renderPasswordStep() {
  return `
    <form id="settings-password-form">
      <p class="settings-help">${t("tgSetup.passwordHelp")}</p>
      <label class="settings-field">
        <span>${t("tgSetup.passwordLabel")}</span>
        <input id="settings-password" name="password" type="password" autocomplete="off" required />
      </label>
      ${renderTelegramError()}
      <button type="submit" class="modal__btn modal__btn--primary" ${setupState.telegramBusy ? "disabled" : ""}>
        ${setupState.telegramBusy ? t("tgSetup.verifying") : t("tgSetup.passwordSubmit")}
      </button>
      <button type="button" id="settings-start-over" class="modal__btn">${t("tgSetup.startOver")}</button>
    </form>
  `;
}

function renderLinkedStep() {
  return `
    <p><span class="patina-badge">${t("tgSetup.linkedBadge")}</span></p>
    <p class="settings-help">${t("tgSetup.linkedBody")}</p>
    ${renderTelegramError()}
    <div class="settings-actions">
      <button type="button" id="settings-relink" class="modal__btn" ${setupState.telegramBusy ? "disabled" : ""}>${t("tgSetup.relinkButton")}</button>
      <button type="button" id="settings-unlink" class="modal__btn modal__btn--danger" ${setupState.telegramBusy ? "disabled" : ""}>
        ${setupState.telegramBusy ? t("tgSetup.unlinking") : t("tgSetup.unlinkButton")}
      </button>
    </div>
  `;
}

function renderLoadingStep() {
  return `<p class="settings-help">${t("tgSetup.telegramStatusChecking")}</p>`;
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
    stepRenderers[setupState.telegramStep] || renderCredentialsStep
  )();
  return `
    <section class="settings-card">
      <h2>${t("tgSetup.telegramTitle")}</h2>
      <p class="settings-help">${t("tgSetup.telegramIntro")}</p>
      ${body}
    </section>
  `;
}

/** Reset to a clean, error-free step and re-render - shared by every "go back / start over" button. */
function goToTelegramStep(root, step, { error = null } = {}) {
  setupState.telegramStep = step;
  setupState.telegramError = error;
  setupState.telegramBusy = false;
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
    setupState.telegramBusy = true;
    setupState.telegramError = null;
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
    setupState.telegramBusy = true;
    setupState.telegramError = null;
    renderRoot(root);
    try {
      await postTelegramStep("/api/telegram/link/send-code", { phone });
      setupState.phone = phone;
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
    setupState.telegramBusy = true;
    setupState.telegramError = null;
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
    setupState.telegramBusy = true;
    setupState.telegramError = null;
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

  document
    .getElementById("settings-unlink")
    ?.addEventListener("click", async () => {
      // window.confirm() - same pattern archiver-toggle.js already uses for its own irreversible-feeling action (stopping the archiver).
      if (!window.confirm(t("tgSetup.unlinkConfirm"))) return;

      setupState.telegramBusy = true;
      setupState.telegramError = null;
      renderRoot(root);
      try {
        // DELETE /telegram/session (unlike the four POST steps above) has no body, so it doesn't go through postTelegramStep().
        const res = await apiFetch("/api/telegram/session", {
          method: "DELETE",
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(describeError(data.detail));
        }
        // Credentials are kept (see clear_telegram_session()'s own docstring) - land on "phone" rather than
        // "credentials", so relinking doesn't ask for the api_id/api_hash pair again for no reason.
        goToTelegramStep(root, "phone");
      } catch (err) {
        setupState.telegramBusy = false;
        setupState.telegramError = err.message || t("common.error");
        renderRoot(root);
      }
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
      setupState.telegramStep = "credentials";
    } else {
      const status = await res.json();
      setupState.telegramStep = status.has_session
        ? "linked"
        : status.has_credentials
          ? "phone"
          : "credentials";
    }
  } catch {
    setupState.telegramStep = "credentials";
  }
  renderRoot(root);
}

// ---------------------------------------------------------------------------
// Archive database card
// ---------------------------------------------------------------------------

function renderArchiveCard() {
  const status = setupState.archiveStatus;
  // "ok" | "unattached" | "unavailable" | "misconfigured", or undefined if we don't have a status at all
  // (still loading, or the health fetch itself failed - the error below says which).
  const state = status?.archive_status;
  const working = setupState.archiveBusy || setupState.archiveProvisioning;

  let body = "";
  if (setupState.archiveBusy && !status) {
    body = `<p class="settings-help">${t("tgSetup.archiveStatusChecking")}</p>`;
  } else if (state === "ok") {
    body = `<p><span class="patina-badge">${t("tgSetup.archiveStatusReady").replace("{count}", String(status.db_message_count ?? 0))}</span></p>`;
  } else if (state === "unattached") {
    body = `<p class="settings-help">${t("tgSetup.archiveStatusMissing")}</p>`;
  } else if (state === "unavailable") {
    body = `<p class="settings-help">${t("tgSetup.archiveStatusUnavailable")}</p>`;
  } else if (state === "misconfigured") {
    // Permanent, admin-only-fixable state (see HealthOut's own docstring) - deliberately NOT the same "try again shortly" wording as "unavailable" above,
    // and styled to stand out the same way, since retrying can never resolve this.
    body = `<p class="settings-error" role="alert">${t("tgSetup.archiveStatusMisconfigured")}</p>`;
  }
  // (no else: with no status at all we deliberately show nothing but Refresh - see canProvision below)

  // Create is offered ONLY when the API has positively said there is no archive yet ("unattached").
  // Not for "unavailable": that means a reference already exists (it just can't be reached right now),
  // and POST /archive/provision refuses any account that already has one (409), so offering the button there just walks the person into a guaranteed error.
  // Not when the status is unknown either - we shouldn't offer to create something we haven't confirmed is missing.
  const canProvision = state === "unattached";

  const provisionButton = canProvision
    ? `<button type="button" id="settings-provision-btn" class="modal__btn modal__btn--primary" ${working ? "disabled" : ""}>
        ${setupState.archiveProvisioning ? t("tgSetup.provisioning") : t("tgSetup.provisionButton")}
      </button>`
    : "";

  // Error goes AFTER the button row (see .settings-actions in base.css) so it never rearranges or pushes the buttons - it just appears beneath them.
  const errorHtml = setupState.archiveError
    ? `<p class="settings-error" role="alert">${escapeHtml(setupState.archiveError)}</p>`
    : "";

  return `
    <section class="settings-card">
      <h2>${t("tgSetup.archiveTitle")}</h2>
      <p class="settings-help">${t("tgSetup.archiveIntro")}</p>
      ${body}
      <div class="settings-actions">
        ${provisionButton}
        <button type="button" id="settings-refresh-archive" class="settings-refresh-btn" ${working ? "disabled" : ""}>${t("tgSetup.refreshStatus")}</button>
      </div>
      ${errorHtml}
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
  setupState.archiveBusy = true;
  if (!preserveError) setupState.archiveError = null;
  renderRoot(root);
  try {
    const res = await apiFetch("/api/health");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setupState.archiveError = describeError(data.detail);
    } else {
      setupState.archiveStatus = await res.json();
      if (!preserveError) setupState.archiveError = null;
    }
  } catch {
    setupState.archiveError = t("common.error");
  }
  setupState.archiveBusy = false;
  renderRoot(root);
}

function attachArchiveHandlers(root) {
  document
    .getElementById("settings-refresh-archive")
    ?.addEventListener("click", () => loadArchiveStatus(root));
  document
    .getElementById("settings-provision-btn")
    ?.addEventListener("click", async () => {
      setupState.archiveProvisioning = true;
      setupState.archiveError = null;
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
          setupState.archiveError = describeError(data.detail);
        }
      } catch {
        provisionFailed = true;
        setupState.archiveError = t("common.error");
      }
      setupState.archiveProvisioning = false;
      await loadArchiveStatus(root, { preserveError: provisionFailed });
    });
}

// ---------------------------------------------------------------------------
// Completion banner - new relative to the old in-SPA Settings tab,
// since a standalone page benefits from a clear "you're done, here's where to go next" moment in a way a tab that's always one click away from Chats anyway didn't need.
// ---------------------------------------------------------------------------

function renderCompletionBanner() {
  const archiveReady = setupState.archiveStatus?.archive_status === "ok";
  if (setupState.telegramStep !== "linked" || !archiveReady) return "";
  return `
    <div class="setup-cta">
      <p>${t("tgSetup.allSetBody")}</p>
      <a href="/index.html" class="modal__btn modal__btn--success">${t("tgSetup.continueToChats")}</a>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Root render / init
// ---------------------------------------------------------------------------

function renderRoot(root) {
  root.innerHTML = `${renderCompletionBanner()}${renderTelegramCard()}${renderArchiveCard()}`;
  attachTelegramHandlers(root);
  attachArchiveHandlers(root);
}

document.addEventListener("DOMContentLoaded", () => {
  // No hasActiveSession() gate like login.js/register.js have (those bounce AWAY if already logged in) - this page needs the opposite:
  // it requires a session, and the very first apiFetch() call below (inside loadTelegramStatus/loadArchiveStatus)
  // transparently gets one via the httpOnly refresh cookie, or redirects to /login.html itself if that fails
  // (see apiFetch's own docstring in lib/auth.js) - same as index.html's chats.js relies on, just without a nav shell around it.
  const root = document.getElementById("telegram-setup-root");
  if (!root) return;

  renderRoot(root);
  loadTelegramStatus(root);
  loadArchiveStatus(root);
});

document.addEventListener("televault:langchange", () => {
  const root = document.getElementById("telegram-setup-root");
  if (root) renderRoot(root);
});

// "admin" badge next to this page's own wordmark - same reasoning and same UI-only caveat as app.js's copy of this (see lib/auth.js's isAdmin()).
// This page has its own header (web/telegram-setup.html), not index.html's nav rail, so it needs its own listener rather than sharing app.js's
// (a separate ES module entry point - see this file's own top-of-file docstring on why the setup page and the app shell don't share init code).
// Current username, shown under the wordmark - deliberately plain text,
// not a link or button (a quiet reminder of who you're logged in as while testing multiple accounts, not another control).
// See app.js's copy of this exact comment for why it needs its own fetchCurrentUser() call rather than a JWT claim.
document.addEventListener("televault:authchange", async (event) => {
  const adminBadge = document.getElementById("admin-badge");
  if (adminBadge) adminBadge.hidden = !isAdmin();

  const usernameEl = document.getElementById("current-username");
  if (usernameEl) {
    if (event.detail.state === "in") {
      const me = await fetchCurrentUser();
      usernameEl.textContent = me ? me.username : "";
    } else {
      usernameEl.textContent = "";
    }
  }
});
