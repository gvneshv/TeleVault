/**
 * Login page (login.html) - a standalone page, not a view mounted inside index.html's app shell.
 * See lib/auth.js's module docstring for why the three pages
 * (login.html, register.html, index.html) are separate rather than one page showing/hiding a login form.
 */

import { t } from "../i18n.js";
import { login, hasActiveSession } from "../lib/auth.js";

document.addEventListener("DOMContentLoaded", async () => {
  // Already have a valid session
  // (e.g. followed a bookmark to /login.html while still logged in, or a still-good httpOnly cookie survived a browser restart) -
  // go straight to the app rather than making them log in again.
  if (await hasActiveSession()) {
    window.location.href = "/index.html";
    return;
  }

  const form = document.getElementById("login-form");
  if (!form) return;

  const usernameInput = document.getElementById("login-username");
  const passwordInput = document.getElementById("login-password");
  const errorEl = document.getElementById("login-error");
  const submitButton = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    submitButton.disabled = true;
    submitButton.textContent = t("login.loggingIn");

    try {
      await login(usernameInput.value, passwordInput.value);
      window.location.href = "/index.html";
      return; // navigating away - no need to restore the button below
    } catch (err) {
      errorEl.textContent = err.message || t("login.error");
      errorEl.hidden = false;
    }

    submitButton.disabled = false;
    submitButton.textContent = t("login.submit");
  });
});
