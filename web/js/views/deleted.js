/**
 * Deleted messages view.
 *
 * Fetches GET /api/deleted (paginated, newest first) — a thin wrapper around /api/messages with only_deleted=True forced server-side (see api/routes/deleted.py).
 * Same row shape as the Messages view, plus an expandable per-row detail panel showing the deletion record:
 * who likely deleted it (self / other / unknown — a best-effort inference, never a certainty) and a plain-language confidence note explaining the guess.
 *
 * The detail panel is fetched lazily via GET /api/messages/{id} the first time a row is expanded,
 * then cached in detailCache for the session — there's no reason to re-fetch a deletion record that can't change.
 *
 * Deliberately does NOT repeat the seal-badge pill per row:
 * every row here is deleted by definition, so the badge would carry no signal
 * (it marks something notable among non-deleted items elsewhere — Chats, Messages — not "all of these," which is just the view's premise).
 * The timestamp is tinted in the seal color instead, a quieter nod to the same meaning.
 *
 * Depends on js/lib/dom.js and js/lib/pagination.js — see index.html for load order.
 * Lazy-initialized by app.js on first tab open, same pattern as messages.js.
 */

const DELETED_PER_PAGE = 50;
// Prefixed for the same reason as MESSAGES_PER_PAGE's sibling constant in messages.js:
// no ES modules here, so all <script>-loaded files share one global lexical scope,
// and a bare "SEARCH_DEBOUNCE_MS" in two files is a page-breaking SyntaxError, not a harmless naming coincidence.
const DELETED_SEARCH_DEBOUNCE_MS = 300;

/** Mutable view state. Re-created fresh; not persisted across reloads. */
const deletedViewState = {
  page: 1,
  q: "",
  lastData: null,
  initialized: false,
  /** message_id -> DeletionOut-shaped detail object, or "error".
   * Populated lazily on first expand; avoids re-fetching a record that can't change. */
  detailCache: new Map(),
};

let deletedSearchDebounceTimer = null;

/**
 * Resolve the best human-readable sender name available.
 * Duplicates SenderOut.resolved_name logic in api/schemas/message.py — see the same note in messages.js's resolveSenderName() about keeping the two in sync.
 *
 * @param {object | null} sender
 * @returns {string}
 */
function resolveDeletedSenderName(sender) {
  if (!sender) return "—";
  if (sender.display_name) return sender.display_name;
  const full = [sender.first_name, sender.last_name].filter(Boolean).join(" ");
  if (full) return full;
  if (sender.username) return `@${sender.username}`;
  return String(sender.sender_id);
}

/** @param {string | null} iso @returns {string} */
function formatDeletedTimestamp(iso) {
  if (!iso) return "—";
  const locale =
    window.TeleVaultI18n.getCurrentLang() === "uk" ? "uk-UA" : "en-US";
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
  const t = window.TeleVaultI18n.t;

  if (detail === "error") {
    return `<div class="deleted-row__detail-error">${t("common.error")}</div>`;
  }
  if (!detail) {
    // Should not normally happen — every row in this view came from a deleted-only query — but the field is nullable in MessageDetail,
    // so it's handled rather than assumed away.
    return `<div class="deleted-row__detail-error">${t("deleted.noRecord")}</div>`;
  }

  const actorLabel = t(`deleted.actor.${detail.deleted_by_inference}`);
  const confidenceNote = detail.inference_confidence
    ? `<p class="deleted-row__confidence">${window.TeleVaultDom.escapeHtml(detail.inference_confidence)}</p>`
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
 *
 * @param {HTMLElement} row - the <li class="deleted-row"> element.
 * @param {number} messageId
 */
async function toggleDeletedRowDetail(row, messageId) {
  const t = window.TeleVaultI18n.t;
  const panel = row.querySelector(".deleted-row__detail");
  const toggleBtn = row.querySelector(".deleted-row__toggle");
  const isOpen = !panel.hidden;

  if (isOpen) {
    panel.hidden = true;
    toggleBtn.textContent = t("deleted.viewDetails");
    return;
  }

  panel.hidden = false;
  toggleBtn.textContent = t("deleted.hideDetails");

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
  // The row may have been re-rendered (pagination, language change)
  // while the fetch was in flight — re-query the panel rather than trust the closed-over `panel` reference.
  const currentPanel = row.isConnected
    ? row.querySelector(".deleted-row__detail")
    : null;
  if (currentPanel) currentPanel.innerHTML = renderDeletionDetail(detail);
}

/**
 * Render one deleted message row as an HTML string.
 * @param {object} msg - a MessageOut record from the API.
 * @returns {string}
 */
function renderDeletedRow(msg) {
  const t = window.TeleVaultI18n.t;
  const escapeHtml = window.TeleVaultDom.escapeHtml;

  const chatTypeLabel = msg.chat?.chat_type
    ? t(`common.type.${msg.chat.chat_type}`)
    : "";
  const chatName = msg.chat
    ? escapeHtml(msg.chat.name ?? String(msg.chat.chat_id))
    : "—";

  const text = msg.text
    ? window.TeleVaultDom.highlightMatches(
        escapeHtml(msg.text),
        deletedViewState.q,
      )
    : `<span class="message-row__text--empty">${t("messages.noText")}</span>`;

  return `
    <li class="deleted-row message-row" data-message-id="${msg.id}">
      <div class="message-row__meta">
        <span class="message-row__sender">${escapeHtml(resolveDeletedSenderName(msg.sender))}</span>
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
 * Render the view's current state (rows + pagination) from already-fetched data,
 * without a network re-fetch — used both after loading and after a language change.
 * Note: expanding a row always re-fetches nothing (cache persists across re-renders),
 * but any currently-open panel collapses on re-render, since panel open/closed state isn't part of the cached data.
 *
 * @param {HTMLElement} root
 * @param {object} data - a PaginatedResponse<MessageOut> from the API.
 */
function renderDeletedView(root, data) {
  const t = window.TeleVaultI18n.t;

  if (data.items.length === 0) {
    root.innerHTML = `<div class="empty-state">${t("deleted.empty")}</div>`;
    return;
  }

  const rowsHtml = data.items.map(renderDeletedRow).join("");
  const paginationHtml = window.TeleVaultPagination.render(
    data.page,
    data.pages,
  );

  root.innerHTML = `
    <ul class="message-list">${rowsHtml}</ul>
    ${paginationHtml}
  `;

  root.querySelectorAll(".deleted-row__toggle").forEach((btn) => {
    const row = btn.closest(".deleted-row");
    const messageId = Number(row.dataset.messageId);
    btn.addEventListener("click", () => toggleDeletedRowDetail(row, messageId));
  });

  window.TeleVaultPagination.attach(root, (delta) => {
    deletedViewState.page += delta;
    loadDeleted(root);
  });
}

/** Fetch one page of deleted messages (with current search applied), cache it, and render it. */
async function loadDeleted(root) {
  const t = window.TeleVaultI18n.t;
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
  const t = window.TeleVaultI18n.t;

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
    }, DELETED_SEARCH_DEBOUNCE_MS);
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

window.TeleVaultDeletedView = { init: initDeletedView };

// Re-render the already-fetched page in the new language.
// Any open detail panel collapses (see renderDeletedView's note) — a minor,
// rare-case UX trade-off against the complexity of preserving per-row expand state across a full re-render,
// not worth it for how infrequently someone switches language mid-expand.
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
