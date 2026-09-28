/**
 * Settings view.
 *
 * Deliberately minimal right now: one card linking out to the dedicated telegram-setup.html page
 * (see that file's own module docstring for why the Telegram/archive wizard lives there instead of here).
 * This tab is where future account-settings features land as they're built - change password,
 * and whatever else comes up - each as its own card, without this file ever having to hold the whole Telegram wizard's state machine alongside them.
 *
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t } from "../i18n.js";

const settingsViewState = { initialized: false };

function renderRoot(root) {
  root.innerHTML = `
    <section class="settings-card">
      <h2>${t("settings.telegramCardTitle")}</h2>
      <p class="settings-help">${t("settings.telegramCardBody")}</p>
      <a href="/telegram-setup.html" class="modal__btn modal__btn--primary">${t("settings.telegramCardLink")}</a>
    </section>
  `;
}

function initSettingsView() {
  const root = document.getElementById("settings-root");
  if (!root) return;
  if (settingsViewState.initialized) return;
  settingsViewState.initialized = true;

  renderRoot(root);
}

export { initSettingsView };

// Re-render in the new language - same pattern as the other views (e.g. health.js).
document.addEventListener("televault:langchange", () => {
  if (!settingsViewState.initialized) return;
  const root = document.getElementById("settings-root");
  if (root) renderRoot(root);
});
