/**
 * Registration page (register.html) - a standalone page, same pattern as login.js.
 *
 * Client-side password-match checking only - the actual minimum-length rule (8 characters) is enforced by the backend
 * (api/schemas/auth.py's RegisterIn) and by the input's own minlength attribute below;
 * this file doesn't duplicate that check, so there's exactly one place ("8 characters") to change if it ever does.
 */

import { t } from "../i18n.js";
import { register, hasActiveSession, setAuthState } from "../lib/auth.js";

document.addEventListener("DOMContentLoaded", async () => {
  // Same reasoning as login.js:
  // don't ask an already-logged-in visitor to register again, and don't reveal the form (see data-auth="checking" in register.html) until we know for certain we should.
  if (await hasActiveSession()) {
    window.location.href = "/index.html";
    return;
  }
  setAuthState("out");

  const form = document.getElementById("register-form");
  if (!form) return;

  const inviteInput = document.getElementById("register-invite");
  const usernameInput = document.getElementById("register-username");
  const passwordInput = document.getElementById("register-password");
  const confirmInput = document.getElementById("register-confirm");
  const errorEl = document.getElementById("register-error");
  const submitButton = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;

    if (passwordInput.value !== confirmInput.value) {
      errorEl.textContent = t("register.passwordMismatch");
      errorEl.hidden = false;
      return;
    }

    submitButton.disabled = true;
    submitButton.textContent = t("register.creating");

    try {
      await register(
        inviteInput.value.trim(),
        usernameInput.value,
        passwordInput.value,
      );
      // A brand-new account can't do anything useful yet (no Telegram linked, no archive database) -
      // send it to the dedicated setup page instead of the normally-empty Chats tab.
      // login.js's redirect stays /index.html: a returning user is presumably already set up, and if they're not, Settings links to telegram-setup.html from there too.
      window.location.href = "/telegram-setup.html";
      return; // navigating away - no need to restore the button below
    } catch (err) {
      errorEl.textContent = err.message || t("register.error");
      errorEl.hidden = false;
      // Keep everything the person typed for errors about OTHER fields ("username taken", "invalid invite", rate limits) -
      // retyping two passwords because the username was taken is pure friction.
      // Clear only when the server rejected the password itself (422 validation, e.g. too short): that value must change anyway.
      if (err.status === 422) {
        passwordInput.value = "";
        confirmInput.value = "";
        passwordInput.focus();
      }
    }

    submitButton.disabled = false;
    submitButton.textContent = t("register.submit");
  });
});
