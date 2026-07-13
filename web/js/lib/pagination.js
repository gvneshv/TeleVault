/**
 * Shared pagination component.
 *
 * Every paginated view (Chats, Messages, and eventually Deleted)
 * needs the same prev/next + "page X of Y" control wired to the same PaginatedResponse shape the API always returns.
 * Pulled out here after the second view needed it verbatim — a single bug fix or style change now applies everywhere instead of needing to be repeated per view.
 */

import { t } from "../i18n.js";

/**
 * Build the HTML for a pagination control.
 * Returns an empty string when there's only one page — nothing to paginate, nothing to show.
 *
 * @param {number} page - current page (1-based)
 * @param {number} pages - total page count
 * @returns {string}
 */
function renderPagination(page, pages) {
  if (pages <= 1) return "";

  const pageOfText = t("common.pageOf")
    .replace("{page}", String(page))
    .replace("{pages}", String(pages));

  return `
    <div class="pagination">
      <button class="pagination__btn" data-page-action="prev" ${page <= 1 ? "disabled" : ""}>
        ${t("common.prev")}
      </button>
      <span class="pagination__info">${pageOfText}</span>
      <button class="pagination__btn" data-page-action="next" ${page >= pages ? "disabled" : ""}>
        ${t("common.next")}
      </button>
    </div>
  `;
}

/**
 * Wire up click handlers for a pagination control previously inserted into `root` by renderPagination().
 * Calls `onPageChange(delta)` with +1 or -1.
 *
 * @param {HTMLElement} root - container the pagination markup was rendered into
 * @param {(delta: number) => void} onPageChange
 */
function attachPaginationHandlers(root, onPageChange) {
  root.querySelectorAll("[data-page-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      onPageChange(btn.dataset.pageAction === "next" ? 1 : -1);
    });
  });
}

export { renderPagination as render, attachPaginationHandlers as attach };
