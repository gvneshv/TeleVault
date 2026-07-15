/**
 * Deleted messages view.
 *
 * Fetches GET /api/deleted (paginated, newest first) — a thin wrapper around /api/messages with only_deleted=True forced server-side (see api/routes/deleted.py).
 * Same row shape as the Messages view, plus an expandable per-row detail panel showing the deletion record:
 * who likely deleted it (self / other / unknown — a best-effort inference, never a certainty) and a plain-language confidence note explaining the guess.
 *
 * The detail panel is fetched lazily via GET /api/messages/{id} the first time a row is expanded,
 * then cached in detailCache for the session — there's no reason to re-fetch a deletion record that can't change.
 * Expanded rows also survive pagination and language-switch re-renders (see expandedIds / restoreExpandedRows()), reopening from cache rather than collapsing.
 *
 * Deliberately does NOT repeat the seal-badge pill per row:
 * every row here is deleted by definition, so the badge would carry no signal
 * (it marks something notable among non-deleted items elsewhere — Chats, Messages — not "all of these," which is just the view's premise).
 * The timestamp is tinted in the seal color instead, a quieter nod to the same meaning.
 *
 * Imports js/lib/dom.js and js/lib/pagination.js as ES modules.
 * Lazy-initialized by app.js on first tab open, same pattern as messages.js.
 */

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml, highlightMatches } from "../lib/dom.js";
import {
  render as renderPagination,
  attach as attachPagination,
} from "../lib/pagination.js";

const DELETED_PER_PAGE = 50;
// Previously prefixed (DELETED_SEARCH_DEBOUNCE_MS) to dodge a real page-breaking SyntaxError:
// without ES modules, this and messages.js's identical constant name shared one global lexical scope.
// Reverted to a plain name now that this file is a proper ES module with its own scope.
const SEARCH_DEBOUNCE_MS = 300;

/** Mutable view state. Re-created fresh; not persisted across reloads. */
const deletedViewState = {
  page: 1,
  q: "",
  lastData: null,
  initialized: false,
  /** message_id -> DeletionOut-shaped detail object, or "error".
   *  Populated lazily on first expand; avoids re-fetching a record that can't change.
   * */
  detailCache: new Map(),
  /** Set of message_ids whose detail panel is currently expanded.
   *  Persists across re-renders (pagination, language change) so a row that was open reopens automatically if it's rendered again — see restoreExpandedRows().
   * */
  expandedIds: new Set(),
};

let deletedSearchDebounceTimer = null;

/** @param {string | null} iso @returns {string} */
function formatDeletedTimestamp(iso) {
  if (!iso) return "—";
  const locale = getCurrentLang() === "uk" ? "uk-UA" : "en-US";
  try {
    return new Date(iso).toLocaleString(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

/**
 * Render the deletion-detail panel's inner content once fetched (or errored).
 * Separated from the fetch so it can be called both after a successful fetch and, unmodified, from the cache on a second expand.
 *
 * @param {object | "error"} detail - the `deletion` sub-object from
 *   MessageDetail, or the string "error" if the fetch failed.
 * @returns {string}
 */
function renderDeletionDetail(detail) {
  if (detail === "error") {
    return `<div class="deleted-row__detail-error">${t("common.error")}</div>`;
  }
  if (!detail) {
    // Should not normally happen — every row in this view came from a deleted-only query — but the field is nullable in MessageDetail,
    // so it's handled rather than assumed away.
    return `<div class="deleted-row__detail-error">${t("deleted.noRecord")}</div>`;
  }

  const actorLabel = t(`deleted.actor.${detail.deleted_by_inference}`);
  // The confidence note is translated client-side from deleted_by_inference,
  // NOT read from detail.inference_confidence — that field is a fixed English string written by the backend (db/queries.py's flag_deleted())
  // and can't respond to the UI's language setting.
  // It's still returned by the API for anyone consuming it directly; the web UI just doesn't display it.
  // "unknown" has no confidence key — nothing to explain about not guessing.
  const confidenceNote =
    detail.deleted_by_inference !== "unknown"
      ? `<p class="deleted-row__confidence">${escapeHtml(t(`deleted.confidence.${detail.deleted_by_inference}`))}</p>`
      : "";

  return `
    <div class="deleted-row__detail-body">
      <span class="info-badge">${actorLabel}</span>
      ${confidenceNote}
    </div>
  `;
}

/**
 * Toggle a row's detail panel open/closed, fetching the deletion record on first open only (see detailCache).
 * Updates expandedIds so the panel can reopen automatically if this row is re-rendered later (pagination, language change) — see restoreExpandedRows().
 *
 * @param {HTMLElement} row - the <li class="deleted-row"> element.
 * @param {number} messageId
 */
async function toggleDeletedRowDetail(row, messageId) {
  const panel = row.querySelector(".deleted-row__detail");
  const isOpen = !panel.hidden;

  if (isOpen) {
    panel.hidden = true;
    row.querySelector(".deleted-row__toggle").textContent = t(
      "deleted.viewDetails",
    );
    deletedViewState.expandedIds.delete(messageId);
    return;
  }

  deletedViewState.expandedIds.add(messageId);
  await openDeletedRowDetail(row, messageId);
}

/**
 * Open a row's detail panel and populate it — from detailCache if already fetched, otherwise via GET /api/messages/{id}.
 * Does NOT touch expandedIds;
 * callers decide whether this open should be tracked (toggleDeletedRowDetail always does; restoreExpandedRows doesn't need to,
 * since the id is already in the set it's iterating).
 *
 * @param {HTMLElement} row
 * @param {number} messageId
 */
async function openDeletedRowDetail(row, messageId) {
  const panel = row.querySelector(".deleted-row__detail");

  panel.hidden = false;
  row.querySelector(".deleted-row__toggle").textContent = t(
    "deleted.hideDetails",
  );

  if (deletedViewState.detailCache.has(messageId)) {
    panel.innerHTML = renderDeletionDetail(
      deletedViewState.detailCache.get(messageId),
    );
    return;
  }

  panel.innerHTML = `<div class="deleted-row__detail-loading">${t("common.loading")}</div>`;

  let detail;
  try {
    const res = await fetch(`/api/messages/${messageId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    detail = data.deletion ?? null;
  } catch {
    detail = "error";
  }

  deletedViewState.detailCache.set(messageId, detail);
  // The row may have been re-rendered (pagination, language change) while the fetch was in flight — re-query the panel rather than trust the closed-over `panel` reference.
  const currentPanel = row.isConnected
    ? row.querySelector(".deleted-row__detail")
    : null;
  if (currentPanel) currentPanel.innerHTML = renderDeletionDetail(detail);
}

/**
 * After a render, reopen any row whose id is in expandedIds — restoring expansion state across pagination and language-change re-renders.
 * Only matches rows actually present on the current page;
 * a message expanded on a different page simply has no matching row here;
 * harmless no-op.
 *
 * Every id in expandedIds must already be in detailCache (you can only add to expandedIds via a completed open),
 * so this never triggers a fetch — purely synchronous re-population from cache.
 *
 * Edge case, not handled:
 * if a re-render happens while a just-opened row's first fetch is still in flight (id added to expandedIds, but detailCache doesn't have it yet),
 * this can trigger a second concurrent fetch for the same id.
 * Harmless — both writes the same result to detailCache — not worth de-duplication logic for how rarely a re-render and an in-flight fetch would overlap in a single-user tool.
 *
 * @param {HTMLElement} root
 */
function restoreExpandedRows(root) {
  if (deletedViewState.expandedIds.size === 0) return;

  root.querySelectorAll(".deleted-row").forEach((row) => {
    const messageId = Number(row.dataset.messageId);
    if (deletedViewState.expandedIds.has(messageId)) {
      openDeletedRowDetail(row, messageId);
    }
  });
}

/**
 * Render one deleted message row as an HTML string.
 * @param {object} msg - a MessageOut record from the API.
 * @returns {string}
 */
function renderDeletedRow(msg) {
  const chatTypeLabel = msg.chat?.chat_type
    ? t(`common.type.${msg.chat.chat_type}`)
    : "";
  const chatName = msg.chat
    ? escapeHtml(msg.chat.name ?? String(msg.chat.chat_id))
    : "—";

  const text = msg.text
    ? highlightMatches(escapeHtml(msg.text), deletedViewState.q)
    : `<span class="message-row__text--empty">${t("messages.noText")}</span>`;

  return `
    <li class="deleted-row message-row" data-message-id="${msg.id}">
      <div class="message-row__meta">
        <span class="message-row__sender">${escapeHtml(msg.sender?.resolved_name ?? "—")}</span>
        <span class="message-row__chat">
          ${chatName}
          ${chatTypeLabel ? `<span class="info-badge">${chatTypeLabel}</span>` : ""}
        </span>
        <span class="deleted-row__timestamp">${formatDeletedTimestamp(msg.deleted_at)}</span>
        <button class="deleted-row__toggle" type="button">${t("deleted.viewDetails")}</button>
      </div>
      <div class="message-row__text">${text}</div>
      <div class="deleted-row__detail" hidden></div>
    </li>
  `;
}

/**
 * Render the view's current state (rows + pagination) from already-fetched data, without a network re-fetch — used both after loading and after a language change.
 * Previously-expanded rows (see expandedIds) reopen automatically from cache — no re-fetch needed for that either.
 *
 * @param {HTMLElement} root
 * @param {object} data - a PaginatedResponse<MessageOut> from the API.
 */
function renderDeletedView(root, data) {
  if (data.items.length === 0) {
    root.innerHTML = `<div class="empty-state">${t("deleted.empty")}</div>`;
    return;
  }

  const rowsHtml = data.items.map(renderDeletedRow).join("");
  const paginationHtml = renderPagination(data.page, data.pages);

  root.innerHTML = `
    <ul class="message-list">${rowsHtml}</ul>
    ${paginationHtml}
  `;

  root.querySelectorAll(".deleted-row__toggle").forEach((btn) => {
    const row = btn.closest(".deleted-row");
    const messageId = Number(row.dataset.messageId);
    btn.addEventListener("click", () => toggleDeletedRowDetail(row, messageId));
  });

  restoreExpandedRows(root);

  attachPagination(root, data.page, data.pages, (page) => {
    deletedViewState.page = page;
    loadDeleted(root);
  });
}

/** Fetch one page of deleted messages (with current search applied), cache it, and render it. */
async function loadDeleted(root) {
  root.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;

  const params = new URLSearchParams({
    page: String(deletedViewState.page),
    per_page: String(DELETED_PER_PAGE),
  });
  if (deletedViewState.q) params.set("q", deletedViewState.q);

  let data;
  try {
    const res = await fetch(`/api/deleted?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch {
    root.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
    return;
  }

  deletedViewState.lastData = data;
  renderDeletedView(root, data);
}

/**
 * Build the filter bar (search only — no edited toggle here, unlike Messages, since "edited" isn't relevant to why a message is in this view).
 *
 * @param {HTMLElement} filterBarRoot
 * @param {HTMLElement} listRoot
 */
function initDeletedFilterBar(filterBarRoot, listRoot) {
  filterBarRoot.innerHTML = `
    <input
      type="search"
      id="deleted-search"
      class="messages-filter__search"
      placeholder="${t("deleted.searchPlaceholder")}"
    />
  `;

  const searchInput = filterBarRoot.querySelector("#deleted-search");
  searchInput.addEventListener("input", () => {
    clearTimeout(deletedSearchDebounceTimer);
    deletedSearchDebounceTimer = setTimeout(() => {
      deletedViewState.q = searchInput.value.trim();
      deletedViewState.page = 1;
      loadDeleted(listRoot);
    }, SEARCH_DEBOUNCE_MS);
  });
}

/** Entry point called by app.js the first time the Deleted tab is opened. */
function initDeletedView() {
  if (deletedViewState.initialized) return;
  deletedViewState.initialized = true;

  const filterBarRoot = document.getElementById("deleted-filter-bar");
  const listRoot = document.getElementById("deleted-root");
  if (!filterBarRoot || !listRoot) return;

  initDeletedFilterBar(filterBarRoot, listRoot);
  loadDeleted(listRoot);
}

export { initDeletedView };

// Re-render the already-fetched page in the new language.
// Expanded rows (see expandedIds) reopen automatically via renderDeletedView's call to restoreExpandedRows() — no special handling needed here.
document.addEventListener("televault:langchange", () => {
  if (!deletedViewState.initialized) return;

  const filterBarRoot = document.getElementById("deleted-filter-bar");
  const listRoot = document.getElementById("deleted-root");
  if (filterBarRoot) {
    const currentQ = deletedViewState.q;
    initDeletedFilterBar(filterBarRoot, listRoot);
    filterBarRoot.querySelector("#deleted-search").value = currentQ;
  }
  if (listRoot && deletedViewState.lastData) {
    renderDeletedView(listRoot, deletedViewState.lastData);
  }
});
