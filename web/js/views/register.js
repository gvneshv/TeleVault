/**
 * Registration page (register.html) - a standalone page, same pattern as login.js.
 *
 * Client-side password-match checking only - the actual minimum-length rule (8 characters) is enforced by the backend
 * (api/schemas/auth.py's RegisterIn) and by the input's own minlength attribute below;
 * this file doesn't duplicate that check, so there's exactly one place ("8 characters") to change if it ever does.
 */

import { t } from "../i18n.js";
import { register, hasActiveSession } from "../lib/auth.js";

document.addEventListener("DOMContentLoaded", async () => {
  // Same reasoning as login.js: don't ask an already-logged-in visitor to register again.
  if (await hasActiveSession()) {
    window.location.href = "/index.html";
    return;
  }

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
      window.location.href = "/index.html";
      return; // navigating away - no need to restore the button below
    } catch (err) {
      errorEl.textContent = err.message || t("register.error");
      errorEl.hidden = false;
    }

    submitButton.disabled = false;
    submitButton.textContent = t("register.submit");
  });
});
