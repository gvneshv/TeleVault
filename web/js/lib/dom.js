/**
 * Shared DOM helpers used across views.
 *
 * Kept dependency-free and framework-free, same as the rest of web/js/ — no bundler here,
 * so this is loaded via a plain <script> tag and exposes itself on window, same pattern as TeleVaultI18n.
 */

/**
 * Minimal HTML-escaping for text before it's inserted via innerHTML.
 *
 * Used for anything that originates from Telegram or the archive (message text, chat/sender names) — never from this app itself — so it must never be trusted as-is.
 * Uses the browser's own text-node escaping rather than a hand-rolled regex, which is easy to get subtly wrong.
 *
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

window.TeleVaultDom = { escapeHtml };
