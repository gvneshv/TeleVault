/**
 * Order toggle: a single button that flips between "desc" (newest first) and "asc" (oldest first),
 * relabeling itself - the same pattern as the light/dark theme toggle in theme.js, rather than a native <select> with only two options ever in it.
 *
 * Shared by the Messages, Deleted, and Chats views, which each had their own near-identical <select id="X-order"> markup and change-listener wiring.
 */

import { t } from "../i18n.js";

/**
 * @param {string} id - element id for the button (e.g. "messages-order").
 * @param {"asc"|"desc"} order - current order, used to pick the initial label/icon.
 * @param {{descKey?: string, ascKey?: string}} [labelKeys] - override the i18n keys used for the desc/asc labels.
 *   Defaults to common.newestFirst/common.oldestFirst;
 *   chats.js passes its own (chats.mostRecentFirst/chats.leastRecentFirst) since it sorts by last activity, not a message date, and reads better with that wording.
 * @returns {string} HTML for the toggle button.
 */
function renderOrderToggle(id, order, labelKeys = {}) {
  const descKey = labelKeys.descKey ?? "common.newestFirst";
  const ascKey = labelKeys.ascKey ?? "common.oldestFirst";
  const label = order === "asc" ? t(ascKey) : t(descKey);
  const arrow = order === "asc" ? "↑" : "↓";
  return `
    <button
      type="button"
      id="${id}"
      class="tv-order-toggle"
      data-order="${order}"
      aria-label="${label}"
    >
      <span class="tv-order-toggle__arrow" aria-hidden="true">${arrow}</span>
      <span class="tv-order-toggle__label">${label}</span>
    </button>
  `;
}

/**
 * Update an already-rendered toggle button's label/icon/state in place - used after a language change, without needing a full re-render.
 *
 * @param {HTMLElement} button
 * @param {"asc"|"desc"} order
 * @param {{descKey?: string, ascKey?: string}} [labelKeys] - see renderOrderToggle().
 */
function updateOrderToggle(button, order, labelKeys = {}) {
  if (!button) return;
  const descKey = labelKeys.descKey ?? "common.newestFirst";
  const ascKey = labelKeys.ascKey ?? "common.oldestFirst";
  const label = order === "asc" ? t(ascKey) : t(descKey);
  const arrow = order === "asc" ? "↑" : "↓";
  button.dataset.order = order;
  button.setAttribute("aria-label", label);
  button.querySelector(".tv-order-toggle__arrow").textContent = arrow;
  button.querySelector(".tv-order-toggle__label").textContent = label;
}

/**
 * Wire a rendered toggle button's click handler.
 *
 * @param {HTMLElement} container - element containing the button (e.g. the filter bar root).
 * @param {string} id - the button's element id.
 * @param {() => "asc"|"desc"} getOrder - returns the current order from view state.
 * @param {(next: "asc"|"desc") => void} onChange - called with the new order after toggling.
 */
function wireOrderToggle(container, id, getOrder, onChange) {
  const button = container.querySelector(`#${id}`);
  if (!button) return;
  button.addEventListener("click", () => {
    const next = getOrder() === "asc" ? "desc" : "asc";
    updateOrderToggle(button, next);
    onChange(next);
  });
}

export { renderOrderToggle, updateOrderToggle, wireOrderToggle };
