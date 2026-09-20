/**
 * Chat filter: a button that opens a dropdown panel of checkboxes - one per known chat,
 * plus a pinned "All chats" row - letting a view restrict its results to a subset of chats.
 *
 * Shared by the Messages and Deleted views.
 * Each gets its OWN independent instance and selection
 * (filtering Deleted down to one chat doesn't also filter Messages) - see createChatFilter()'s storageKey param.
 *
 * The chat list itself ({chat_id, name} pairs from GET /api/chats/options) is fetched once and cached at module level,
 * since both views want the same list and it only changes when a brand-new chat first appears - not worth re-fetching per view, per open.
 *
 * Selection convention: an empty selection means "All chats" (no chat_ids filter sent to the API).
 * There is no separate "nothing selected" state - clicking "All chats" or unchecking every box both land on the same empty-selection, no-filter state,
 * which is the simplest way to avoid a confusing "you've selected zero chats so nothing will show" trap.
 *
 * Selection persists in localStorage per view (this app's own long-lived UI, not a sandboxed artifact - localStorage is the right tool here) so it survives a reload.
 * Whether to persist at all (vs. always resetting to "All chats") is exactly the kind of toggle that belongs on a future settings page
 * - not built here since there's only one behaviour to choose from today.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "./dom.js";
import { apiFetch } from "./auth.js";

let cachedChatOptions = null;
let chatOptionsPromise = null;

/** Fetch the full {chat_id, name}[] list once; later calls reuse the cached result (or in-flight promise). */
function fetchChatOptions() {
  if (cachedChatOptions) return Promise.resolve(cachedChatOptions);
  if (chatOptionsPromise) return chatOptionsPromise;
  chatOptionsPromise = apiFetch("/api/chats/options")
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((options) => {
      cachedChatOptions = options;
      return options;
    })
    .catch(() => {
      // Don't cache a failure - the next open should get a real retry, not be stuck empty forever.
      chatOptionsPromise = null;
      return [];
    });
  return chatOptionsPromise;
}

function loadPersisted(storageKey) {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

/**
 * @param {object} opts
 * @param {string} opts.id - unique DOM id prefix (e.g. "messages-chat-filter"). Must be unique on the page.
 * @param {string} opts.storageKey - localStorage key this instance's selection is persisted under.
 * @param {(chatIds: number[]) => void} opts.onChange - called with the new selection (empty array = "All chats") whenever it changes.
 * @returns {{mount: (parent: HTMLElement) => Promise<void>, getSelectedIds: () => number[]}}
 */
function createChatFilter({ id, storageKey, onChange }) {
  const selected = loadPersisted(storageKey);
  let options = [];
  let open = false;
  let searchText = "";
  let mountEl = null;

  function persist() {
    try {
      localStorage.setItem(storageKey, JSON.stringify([...selected]));
    } catch {
      // Best-effort - a full/blocked localStorage shouldn't break filtering itself, just the persistence of it.
    }
  }

  function label() {
    if (selected.size === 0) return t("chatFilter.allChats");
    if (selected.size === 1) {
      const [onlyId] = selected;
      const match = options.find((o) => o.chat_id === onlyId);
      return match?.name ?? String(onlyId);
    }
    return t("chatFilter.nChatsSelected").replace("{n}", String(selected.size));
  }

  function visibleOptions() {
    const needle = searchText.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((o) => (o.name ?? "").toLowerCase().includes(needle));
  }

  function renderOptionRow(o) {
    return `
      <label class="tv-checkbox chat-filter__option">
        <input type="checkbox" data-chat-id="${o.chat_id}" ${selected.has(o.chat_id) ? "checked" : ""} />
        <span class="tv-checkbox__box"></span>
        <span class="chat-filter__option-name">${escapeHtml(o.name ?? String(o.chat_id))}</span>
      </label>
    `;
  }

  function renderList() {
    const visible = visibleOptions();
    if (options.length === 0) {
      return `<div class="chat-filter__empty">${t("chats.empty")}</div>`;
    }
    if (visible.length === 0) {
      return `<div class="chat-filter__empty">${t("chatFilter.noMatches")}</div>`;
    }
    return visible.map(renderOptionRow).join("");
  }

  function renderPanel() {
    return `
      <div class="chat-filter__panel" role="dialog" aria-label="${t("chatFilter.allChats")}">
        <input
          type="search"
          class="chat-filter__search"
          placeholder="${t("chatFilter.searchPlaceholder")}"
          value="${escapeHtml(searchText)}"
        />
        <label class="tv-checkbox chat-filter__option chat-filter__option--all">
          <input type="checkbox" data-action="all-chats" ${selected.size === 0 ? "checked" : ""} />
          <span class="tv-checkbox__box"></span>
          <span class="chat-filter__option-name">${t("chatFilter.allChats")}</span>
        </label>
        <div class="chat-filter__list">${renderList()}</div>
      </div>
    `;
  }

  function render() {
    return `
      <div class="chat-filter" id="${id}">
        <button
          type="button"
          class="chat-filter__toggle"
          id="${id}-toggle"
          aria-haspopup="true"
          aria-expanded="${open}"
        >
          <span class="chat-filter__label">${escapeHtml(label())}</span>
          <span class="chat-filter__chevron" aria-hidden="true"></span>
        </button>
        ${open ? renderPanel() : ""}
      </div>
    `;
  }

  function updateLabel() {
    mountEl
      ?.querySelector(".chat-filter__label")
      ?.replaceChildren(document.createTextNode(label()));
  }

  function updateList() {
    const listEl = mountEl?.querySelector(".chat-filter__list");
    if (listEl) listEl.innerHTML = renderList();
    const allBox = mountEl?.querySelector('[data-action="all-chats"]');
    if (allBox) allBox.checked = selected.size === 0;
  }

  function rerenderRoot() {
    if (!mountEl) return;
    mountEl.innerHTML = render();
    wire();
  }

  // Attached to `document` only while the panel is open, and removed the moment it closes
  // (by any means - outside click, Escape, or the toggle button itself) - see wire() below.
  // Keeps this to at most one live document-level listener per instance at a time, rather than accumulating one per render.
  function handleOutsideClick(e) {
    if (!mountEl || mountEl.contains(e.target)) return; // click was inside our own dropdown - its own handlers deal with it
    close();
  }

  function handleEscape(e) {
    if (e.key !== "Escape") return;
    close();
    mountEl?.querySelector(".chat-filter__toggle")?.focus();
  }

  function attachOutsideListeners() {
    document.addEventListener("click", handleOutsideClick);
    document.addEventListener("keydown", handleEscape);
  }

  function detachOutsideListeners() {
    document.removeEventListener("click", handleOutsideClick);
    document.removeEventListener("keydown", handleEscape);
  }

  function close() {
    if (!open) return;
    open = false;
    detachOutsideListeners();
    rerenderRoot();
  }

  function wire() {
    const root = mountEl.querySelector(`#${id}`);
    if (!root) return;

    root.querySelector(`#${id}-toggle`).addEventListener("click", (e) => {
      e.stopPropagation();
      open = !open;
      if (open) attachOutsideListeners();
      else detachOutsideListeners();
      rerenderRoot();
    });

    if (!open) return;

    const searchInput = root.querySelector(".chat-filter__search");
    searchInput?.addEventListener("input", () => {
      searchText = searchInput.value;
      updateList(); // only the list is rebuilt - the search input itself is left alone, so focus/cursor position survive every keystroke.
    });

    // Delegated on the panel rather than one listener per checkbox,
    // since updateList() replaces .chat-filter__list's innerHTML on every keystroke - a per-row listener would need re-attaching after every one of those;
    // a single listener on a stable ancestor doesn't.
    root
      .querySelector(".chat-filter__panel")
      .addEventListener("change", (e) => {
        const box = e.target;
        if (!(box instanceof HTMLInputElement) || box.type !== "checkbox")
          return;

        if (box.dataset.action === "all-chats") {
          // Clicking "All chats" always resets to the empty (= no filter) selection,
          // rather than being a true two-way toggle - unchecking it while it's the only thing checked would just mean "select nothing,"
          // which already means "All" here, so there's nothing else for it to do.
          selected.clear();
        } else {
          const chatId = Number(box.dataset.chatId);
          if (box.checked) selected.add(chatId);
          else selected.delete(chatId);
        }
        persist();
        onChange([...selected]);
        updateLabel();
        updateList();
      });
  }

  return {
    /** Fetch the chat list (if not already cached) and do the initial render into `parent`. */
    async mount(parent) {
      mountEl = parent;
      options = await fetchChatOptions();
      rerenderRoot();
    },

    getSelectedIds() {
      return [...selected];
    },
  };
}

export { createChatFilter };
