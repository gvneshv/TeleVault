/**
 * Chats list view.
 *
 * Fetches GET /api/chats (paginated) and renders each chat as a row: name, chat-type badge, message/deleted counts, and a preview of the most recent message.
 * This is the landing view, so it self-initializes on DOMContentLoaded rather than waiting for a nav click.
 *
 * Imports js/lib/dom.js (escapeHtml) and js/lib/pagination.js (render/attach) as ES modules.
 *
 * Scope, deliberately: list + pagination only. Clicking a row does nothing yet - there's no single-chat or filtered-messages view to send it to.
 * Each row still carries `data-chat-id` so that wiring is a one-line addition once a per-chat view exists, instead of a re-render change here.
 *
 * State is kept minimal and re-fetched fresh on every page change;
 * nothing is cached client-side beyond the last page (see lastRender below, kept only for language-switch re-rendering).
 * This is a personal single-user archive, not a high-traffic API,
 * so the extra request per page turn is not a real cost - and it keeps this file free of cache-invalidation logic it doesn't need yet.
 */

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { apiFetch } from "../lib/auth.js";
import { renderArchiveErrorState } from "../lib/archive-error.js";
import { renderOrderToggle, wireOrderToggle } from "../lib/order-toggle.js";
import {
  render as renderPagination,
  attach as attachPagination,
} from "../lib/pagination.js";

const CHATS_PER_PAGE = 50;

/** Mutable view state. Re-created fresh; not persisted across reloads. */
const chatsViewState = {
  page: 1,
  order: "desc",
  /**
   * The last thing loadChats() actually rendered, kept so a later language change can redraw it WITHOUT re-fetching - a tagged union rather than three separate flags,
   * so redraw() below (used by both the initial load and the "televault:langchange" listener) has exactly one thing to branch on instead of three that could,
   * through a future edit, end up simultaneously set or all unset:
   *   { type: "data", data }      - a successful page (renderChatsView() below can redraw it)
   *   { type: "error", detail }   - a structured API error
   *                               (see lib/archive-error.js's own docstring for why this is exactly the same code path
   *                               chats.js/messages.js/deleted.js/stats.js all share, and what turns `detail` into displayed text)
   *   { type: "networkError" }    - fetch() itself threw, so there's no response body to describe at all
   *   null                        - nothing loaded yet (before the very first loadChats() call resolves)
   */
  lastRender: null,
};

/**
 * Format an ISO 8601 datetime string using the current UI language's locale.
 * Returns an em dash for null/undefined - some chats have no messages yet.
 *
 * @param {string | null} iso
 * @returns {string}
 */
function formatChatTimestamp(iso) {
  if (!iso) return "-";
  const locale = getCurrentLang() === "uk" ? "uk-UA" : "en-US";
  try {
    return new Date(iso).toLocaleString(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    // Malformed date from the API shouldn't crash the row - fall back to the raw string.
    return iso;
  }
}

/**
 * Build the initials shown in a chat's avatar circle.
 * Falls back to "?" for chats with no name (possible for some private chats where Telegram never supplied one).
 *
 * @param {string | null} name
 * @returns {string}
 */
function chatInitials(name) {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "?";
}

/**
 * Render one chat row as an HTML string.
 * @param {object} chat - a ChatOut record from the API.
 * @returns {string}
 */
function renderChatRow(chat) {
  const typeKey = `common.type.${chat.chat_type}`;
  const typeLabel = chat.chat_type ? t(typeKey) : "";

  const deletedBadge =
    chat.deleted_count > 0
      ? `<span class="seal-badge">${chat.deleted_count} ${t("chats.deletedLabel")}</span>`
      : "";

  const preview = chat.last_message_preview
    ? escapeHtml(chat.last_message_preview)
    : `<span class="chat-row__preview--empty">${t("chats.noPreview")}</span>`;

  return `
    <li class="chat-row" data-chat-id="${chat.chat_id}">
      <div class="chat-row__avatar" aria-hidden="true">${chatInitials(chat.name)}</div>
      <div class="chat-row__body">
        <div class="chat-row__top">
          <span class="chat-row__name">${escapeHtml(chat.name ?? String(chat.chat_id))}</span>
          ${typeLabel ? `<span class="info-badge">${typeLabel}</span>` : ""}
        </div>
        <div class="chat-row__preview">${preview}</div>
      </div>
      <div class="chat-row__stats">
        <span class="chat-row__count">${chat.message_count} ${t("chats.messagesLabel")}</span>
        ${deletedBadge}
        <span class="chat-row__timestamp">${formatChatTimestamp(chat.last_message_at)}</span>
      </div>
    </li>
  `;
}

/**
 * Render the view's current state (rows + pagination) from already-fetched data. Pulled out of loadChats() so a language change can call this again
 * with the cached page instead of hitting the API a second time.
 *
 * @param {HTMLElement} root
 * @param {object} data - a PaginatedResponse<ChatOut> from the API.
 */
function renderChatsView(root, data) {
  if (data.items.length === 0) {
    root.innerHTML = `<div class="empty-state">${t("chats.empty")}</div>`;
    return;
  }

  const rowsHtml = data.items.map(renderChatRow).join("");
  const paginationHtml = renderPagination(data.page, data.pages);

  root.innerHTML = `
    <ul class="chat-list">${rowsHtml}</ul>
    ${paginationHtml}
  `;

  attachPagination(root, data.page, data.pages, (page) => {
    chatsViewState.page = page;
    loadChats(root);
  });
}

const CHATS_ORDER_LABEL_KEYS = {
  descKey: "chats.mostRecentFirst",
  ascKey: "chats.leastRecentFirst",
};

/**
 * Wire up the chats filter bar's sort-order toggle.
 * @param {HTMLElement} filterBarRoot
 * @param {HTMLElement} listRoot
 */
function initChatsFilterBar(filterBarRoot, listRoot) {
  filterBarRoot.innerHTML = `
    ${renderOrderToggle("chats-order", chatsViewState.order, CHATS_ORDER_LABEL_KEYS)}
  `;

  wireOrderToggle(
    filterBarRoot,
    "chats-order",
    () => chatsViewState.order,
    (next) => {
      chatsViewState.order = next;
      chatsViewState.page = 1;
      loadChats(listRoot);
    },
  );
}

/**
 * Render chatsViewState.lastRender's current value into `root` - whatever it is
 * (data, error, or network error; see that field's own comment above for the three shapes).
 * Shared by loadChats() below (right after setting lastRender) and the "televault:langchange" listener at the bottom of this file
 * (redrawing the SAME lastRender value again, in whatever language is now active, without a re-fetch) -
 * one render path instead of two that could drift apart, which is exactly what used to leave an error message frozen in its original language after a language switch:
 * the old code only ever re-rendered here when lastData was set, silently doing nothing for an error state.
 */
function redraw(root) {
  const r = chatsViewState.lastRender;
  if (!r) return;
  if (r.type === "data") {
    renderChatsView(root, r.data);
  } else if (r.type === "error") {
    renderArchiveErrorState(root, r.detail);
  } else {
    root.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
  }
}

/** Fetch one page of chats, cache it, and render it. */
async function loadChats(root) {
  root.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;

  try {
    const res = await apiFetch(
      `/api/chats?page=${chatsViewState.page}&per_page=${CHATS_PER_PAGE}&order=${chatsViewState.order}`,
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      chatsViewState.lastRender = { type: "error", detail: body.detail };
      redraw(root);
      return;
    }
    const data = await res.json();
    chatsViewState.lastRender = { type: "data", data };
    redraw(root);
  } catch {
    // Genuine network/connectivity failure - no response body to describe, so the generic message stays.
    chatsViewState.lastRender = { type: "networkError" };
    redraw(root);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("chats-root");
  const filterBarRoot = document.getElementById("chats-filter-bar");
  if (filterBarRoot) initChatsFilterBar(filterBarRoot, root);
  if (root) loadChats(root);
});

// Re-render whatever's currently shown (data, error, or network error - see chatsViewState.lastRender's own comment) in the new language - no re-fetch needed,
// since only the labels/message text change, not the underlying data or error condition itself.
document.addEventListener("televault:langchange", () => {
  const root = document.getElementById("chats-root");
  const filterBarRoot = document.getElementById("chats-filter-bar");
  if (filterBarRoot) {
    initChatsFilterBar(filterBarRoot, root);
  }
  if (root) redraw(root);
});
