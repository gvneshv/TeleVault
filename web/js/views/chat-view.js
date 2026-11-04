/**
 * The chat view: a panel on the left, a read-only conversation on the right (claude/chat-view-spec.md).
 *
 * It replaces the old Chats, Messages and Deleted pages:
 *   - the PANEL lists chats (GET /api/chats), or - with the switch on "Messages" / "Deleted" -
 *     the cross-chat message feed (GET /api/messages) and the deleted-message list (GET /api/deleted), narrowed by the search box.
 *     Clicking a result opens that chat at that message.
 *   - the CONVERSATION shows one chat the way Telegram would, from GET /api/chats/{id}/window:
 *     deleted messages stay in their place under an amber "Deleted ... preserved" chip, edited ones carry a patina "Edited N times" chip that opens the version history.
 *
 * Moving through the archive
 *   Routes are hash routes carrying ids only (lib/router.js): #/chats/<id> and #/chats/<id>/m/<messageId>.
 *   The window API is cursor-based (keyset on date + id), not paged: the first request returns the newest messages, or the neighbours of an anchor;
 *   scrolling up asks for `before=<first id>`, scrolling down for `after=<last id>`, until has_older / has_newer say there is no more.
 *
 * What is deliberately not here yet (later steps of the spec): the All/Edited/Deleted filter, search inside a chat and jump-to-date in the header
 * (the window endpoint has no filters yet), the resizable panel, and the final phone layout.
 * The old pages' "whole word", "edited only", order and chat-filter controls are not carried over either.
 *
 * Rendering style follows the other views: markup is built as strings, EVERYTHING that comes from Telegram or the archive goes through escapeHtml(),
 * and the last fetched state is kept so a language change redraws without a request.
 * Message detail (edit history, deletion record) is fetched lazily on first click from GET /api/messages/{id} and cached for the page's lifetime:
 * a stored edit history only ever grows, and the cache entry is dropped when a refresh sees the message change.
 *
 * Privacy: this only DISPLAYS what the account owner already archived.
 * Nothing here collects anything, URLs carry ids only, and the two write actions (block, delete stored data) only reduce what TeleVault keeps.
 */

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml, highlightMatches } from "../lib/dom.js";
import { apiFetch } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";
import { renderArchiveErrorState } from "../lib/archive-error.js";
import { renderIcons } from "../lib/icons.js";
import {
  showAlert,
  showConfirm,
  showConfirmWithCheckbox,
} from "../lib/dialog.js";
import { parseChatRoute, chatRouteHash } from "../lib/router.js";

const CHATS_PER_PAGE = 100;
const RESULTS_PER_PAGE = 30;
const SEARCH_DEBOUNCE_MS = 300;
// Start fetching the next batch when the scroll position is this close to the end of what is loaded (px).
const SCROLL_LOAD_MARGIN = 300;
// How long the message a search hit opened is outlined.
const ANCHOR_HIGHLIGHT_MS = 2500;

/** Everything the view remembers.
 *  Not persisted: a reload starts from the hash route alone. */
const state = {
  /** What the panel lists: "chats" | "messages" | "deleted". */
  mode: "chats",
  /** The search box's text, trimmed when sent. */
  q: "",

  /** Panel: chats mode.
   *  `items` grows page by page. */
  chats: { items: [], page: 0, pages: 1, loading: false },
  /** Panel: messages / deleted mode. */
  results: { items: [], page: 0, pages: 1, total: 0, loading: false },
  /** Bumped on every panel (re)load; a response is drawn only if it belongs to the newest one. */
  panelSeq: 0,
  /** Set when the panel's own request failed: { type: "error", detail } or { type: "networkError" }. */
  panelError: null,

  /** The route being shown, or null before the first one. */
  route: null,
  /** The open chat's ChatOut record. */
  chat: null,
  /** The loaded part of the conversation, OLDEST FIRST. */
  messages: [],
  hasOlder: false,
  hasNewer: false,
  /** Internal id of the message to scroll to and outline, until it has been shown once. */
  anchorId: null,
  /** "loading" | "ready" | "error" | "networkError" | "none" (no chat selected). */
  convStatus: "none",
  convError: null,
  /** Bumped every time a chat is opened; guards late responses of an earlier chat. */
  convSeq: 0,
  loadingOlder: false,
  loadingNewer: false,

  /** Chat ids on the ingest blocklist ("Chats not archived"). */
  blocked: new Set(),

  /** message id -> MessageDetail from GET /api/messages/{id}, or "error". */
  detailCache: new Map(),
  /** message ids whose edit-history / deletion note is open. */
  openEdits: new Set(),
  openDeletion: new Set(),
};

let searchTimer = null;
let anchorTimer = null;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** @returns {string} the BCP 47 locale matching the app language. */
function locale() {
  return getCurrentLang() === "uk" ? "uk-UA" : "en-US";
}

/** @param {string | null | undefined} iso @returns {Date | null} null for a missing or unparseable value. */
function toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Local-time "HH:MM". */
function formatTime(iso) {
  const d = toDate(iso);
  return d
    ? d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" })
    : "-";
}

/** Local date and time, as in the old lists: "Oct 31, 2026, 7:24 PM". */
function formatDateTime(iso) {
  const d = toDate(iso);
  return d
    ? d.toLocaleString(locale(), { dateStyle: "medium", timeStyle: "short" })
    : "-";
}

/** A day heading for the conversation: "Saturday, October 31, 2026". */
function formatDay(iso) {
  const d = toDate(iso);
  return d ? d.toLocaleDateString(locale(), { dateStyle: "full" }) : "";
}

/** Local calendar day as a sortable key, used to decide where day headings go. */
function dayKey(iso) {
  const d = toDate(iso);
  return d ? `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}` : "";
}

/**
 * The panel's compact timestamp, like Telegram's chat list:
 * the time for today, otherwise a short date (with the year only when it is not this year).
 */
function formatShortStamp(iso) {
  const d = toDate(iso);
  if (!d) return "";
  const now = new Date();
  if (dayKey(iso) === dayKey(now.toISOString())) return formatTime(iso);
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(
    locale(),
    sameYear
      ? { day: "numeric", month: "short" }
      : { day: "numeric", month: "short", year: "numeric" },
  );
}

/** Initials for an avatar circle; "?" for a chat that has no name. */
function initials(name) {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "?";
}

/**
 * Translate a count-dependent phrase.
 * The key family is `<base>.one|few|many|other`, picked with the language's own plural rules (Ukrainian needs "few"/"many"; English only "one"/"other").
 * A form missing from a table falls back to `.other`.
 *
 * @param {string} base - e.g. "cv.editedTimes".
 * @param {number} n
 * @returns {string} the phrase with "{n}" replaced.
 */
function plural(base, n) {
  const category = new Intl.PluralRules(locale()).select(n);
  const specific = t(`${base}.${category}`);
  const text =
    specific === `${base}.${category}` ? t(`${base}.other`) : specific;
  return text.replace("{n}", String(n));
}

/** Replace "{name}" placeholders in a translated string. */
function fill(text, values) {
  return text.replace(/\{(\w+)\}/g, (whole, key) =>
    key in values ? String(values[key]) : whole,
  );
}

/** @param {object} chat @returns {string} the chat's name, or its id for a chat Telegram never named. */
function chatName(chat) {
  return chat?.name || String(chat?.chat_id ?? "");
}

/** Parse a failed response's JSON body; the structured error, if any, is under `detail`. */
async function readErrorDetail(res) {
  const body = await res.json().catch(() => ({}));
  return body.detail;
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

const panelBody = () => document.getElementById("cv-panel-body");

/** Chats whose name contains the search text (case-insensitive).
 *  With no search text, all loaded chats. */
function visibleChats() {
  const needle = state.q.trim().toLowerCase();
  if (!needle) return state.chats.items;
  return state.chats.items.filter((c) =>
    chatName(c).toLowerCase().includes(needle),
  );
}

/** One row of the chat list.
 *  `<a href>` so keyboard, middle-click and the Back button all work without extra code. */
function renderChatRow(chat) {
  const active = state.route?.chatId === chat.chat_id;
  const edited =
    chat.edited_count > 0
      ? `<span class="patina-badge">${escapeHtml(fill(t("cv.editedCount"), { n: chat.edited_count }))}</span>`
      : "";
  const deleted =
    chat.deleted_count > 0
      ? `<span class="seal-badge">${escapeHtml(fill(t("cv.deletedCount"), { n: chat.deleted_count }))}</span>`
      : "";
  const preview = chat.last_message_preview
    ? escapeHtml(chat.last_message_preview)
    : `<span class="cv-row__empty">${escapeHtml(t("chats.noPreview"))}</span>`;
  return `
    <li>
      <a class="cv-row" href="${chatRouteHash(chat.chat_id)}" ${active ? 'aria-current="true"' : ""}>
        <span class="cv-avatar" aria-hidden="true">${escapeHtml(initials(chat.name))}</span>
        <span class="cv-row__body">
          <span class="cv-row__top">
            <span class="cv-row__name">${escapeHtml(chatName(chat))}</span>
            <span class="cv-row__time">${escapeHtml(formatShortStamp(chat.last_message_at))}</span>
          </span>
          <span class="cv-row__preview">${preview}</span>
          ${edited || deleted ? `<span class="cv-row__chips">${edited}${deleted}</span>` : ""}
        </span>
      </a>
    </li>`;
}

/** One row of the Messages / Deleted result list. */
function renderResultRow(message) {
  const text = message.text
    ? highlightMatches(escapeHtml(message.text), state.q.trim())
    : `<span class="cv-row__empty">${escapeHtml(t("messages.noText"))}</span>`;
  const sender = message.sender?.resolved_name;
  const chip =
    state.mode === "messages" && message.is_deleted
      ? `<span class="seal-badge">${escapeHtml(t("cv.deletedShort"))}</span>`
      : "";
  return `
    <li>
      <a class="cv-row cv-row--result" href="${chatRouteHash(message.chat?.chat_id ?? null, message.id)}">
        <span class="cv-avatar" aria-hidden="true">${escapeHtml(initials(message.chat?.name))}</span>
        <span class="cv-row__body">
          <span class="cv-row__top">
            <span class="cv-row__name">${escapeHtml(message.chat?.name ?? String(message.chat?.chat_id ?? ""))}</span>
            <span class="cv-row__time">${escapeHtml(formatShortStamp(message.date))}</span>
          </span>
          <span class="cv-row__preview cv-row__preview--text">${sender ? `<b>${escapeHtml(sender)}:</b> ` : ""}${text}</span>
          ${chip ? `<span class="cv-row__chips">${chip}</span>` : ""}
        </span>
      </a>
    </li>`;
}

/** Draw the panel's current state.
 *  Pure: reads `state`, writes only the panel body. */
function renderPanel() {
  const root = panelBody();
  if (!root) return;

  const list = state.mode === "chats" ? state.chats : state.results;
  if (state.panelError) {
    if (state.panelError.type === "error") {
      renderArchiveErrorState(root, state.panelError.detail);
    } else {
      root.innerHTML = `<div class="empty-state">${escapeHtml(t("common.error"))}</div>`;
    }
    return;
  }
  if (list.page === 0) {
    root.innerHTML = `<div class="empty-state">${escapeHtml(t("common.loading"))}</div>`;
    return;
  }

  const rows =
    state.mode === "chats"
      ? visibleChats().map(renderChatRow)
      : state.results.items.map(renderResultRow);
  const more = list.page < list.pages;

  if (rows.length === 0 && !more) {
    const key =
      state.mode === "chats"
        ? state.q.trim()
          ? "cv.noChatMatch"
          : "chats.empty"
        : state.mode === "deleted"
          ? "deleted.empty"
          : "messages.empty";
    root.innerHTML = `<div class="empty-state">${escapeHtml(t(key))}</div>`;
    return;
  }

  root.innerHTML = `
    <ul class="cv-list">${rows.join("")}</ul>
    ${more ? `<button type="button" class="cv-more" id="cv-more" ${list.loading ? "disabled" : ""}>${escapeHtml(list.loading ? t("common.loading") : t("cv.loadMore"))}</button>` : ""}`;
  document.getElementById("cv-more")?.addEventListener("click", () => {
    loadPanelPage();
  });
}

/** The search box's placeholder follows both the mode and the language. */
function renderSearchPlaceholder() {
  const input = /** @type {HTMLInputElement | null} */ (
    document.getElementById("cv-search")
  );
  if (!input) return;
  const key = {
    chats: "cv.searchChats",
    messages: "messages.searchPlaceholder",
    deleted: "deleted.searchPlaceholder",
  }[state.mode];
  input.placeholder = t(key);
}

/** The request for one page of what the panel lists in `mode`, for the current search text. */
function panelUrl(mode, page) {
  if (mode === "chats") {
    return `/api/chats?page=${page}&per_page=${CHATS_PER_PAGE}&order=desc`;
  }
  const params = new URLSearchParams({
    page: String(page),
    per_page: String(RESULTS_PER_PAGE),
  });
  if (state.q.trim()) params.set("q", state.q.trim());
  return `/api/${mode === "deleted" ? "deleted" : "messages"}?${params}`;
}

/**
 * Fetch the next page of whatever the panel lists (or the first, after a reset), and draw it.
 *
 * @param {{ reset?: boolean }} [options] reset: start again from page 1 (mode or search text changed).
 */
async function loadPanelPage({ reset = false } = {}) {
  const mode = state.mode;
  const list = mode === "chats" ? state.chats : state.results;
  if (list.loading && !reset) return;

  const seq = ++state.panelSeq;
  if (reset) {
    list.items = [];
    list.page = 0;
    list.pages = 1;
    state.panelError = null;
  }
  list.loading = true;
  renderPanel();

  try {
    const res = await apiFetch(panelUrl(mode, list.page + 1));
    if (seq !== state.panelSeq) return;
    if (!res.ok) {
      const detail = await readErrorDetail(res);
      if (seq !== state.panelSeq) return;
      list.loading = false;
      state.panelError = { type: "error", detail };
      renderPanel();
      return;
    }
    const data = await res.json();
    if (seq !== state.panelSeq) return;
    list.items = list.items.concat(data.items);
    list.page = data.page;
    list.pages = data.pages;
    if ("total" in list) list.total = data.total;
    list.loading = false;
    state.panelError = null;
    renderPanel();

    // The chat list is filtered in the browser (the chats endpoint has no search), so while a search is active every page is needed for the answer to be complete.
    // A personal archive has tens to a few hundred chats, so this is a handful of requests at most.
    if (mode === "chats" && state.q.trim() && list.page < list.pages) {
      loadPanelPage();
    }
  } catch {
    if (seq !== state.panelSeq) return;
    list.loading = false;
    state.panelError = { type: "networkError" };
    renderPanel();
  }
}

/**
 * The tab-switch refresh of the panel: re-fetch every page that is currently loaded and swap the list in only when the answer differs.
 * Nothing is cleared up front and a failure changes nothing, so a flaky connection never wipes a list that is perfectly good
 * (the same rule as lib/refresh.js's silent refresh; the answer is compared as JSON text there too).
 */
async function refetchPanel() {
  const mode = state.mode;
  const list = mode === "chats" ? state.chats : state.results;
  if (list.loading || list.page === 0) return;
  const seq = state.panelSeq;
  const upTo = list.page;
  try {
    let items = [];
    let pages = 1;
    let total = 0;
    let loaded = 0;
    for (let page = 1; page <= upTo; page++) {
      const res = await apiFetch(panelUrl(mode, page));
      if (!res.ok || seq !== state.panelSeq) return;
      const data = await res.json();
      items = items.concat(data.items);
      pages = data.pages;
      total = data.total;
      loaded = page;
      if (page >= data.pages) break;
    }
    if (seq !== state.panelSeq || list.loading) return;
    if (JSON.stringify(items) === JSON.stringify(list.items)) return;
    const body = panelBody();
    const top = body?.scrollTop ?? 0;
    list.items = items;
    list.page = loaded;
    list.pages = pages;
    if ("total" in list) list.total = total;
    renderPanel();
    if (body) body.scrollTop = top;
  } catch {
    // Keep what is shown.
  }
}

/** Switch what the panel lists. */
function setMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;
  document.querySelectorAll(".cv-switch__btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === mode));
  });
  renderSearchPlaceholder();
  loadPanelPage({ reset: true });
}

/** Load the ingest blocklist so the header can show "not archived" and the delete dialog can offer to unblock. */
async function loadBlocklist() {
  try {
    const res = await apiFetch("/api/ingest/blocklist");
    if (!res.ok) return;
    const data = await res.json();
    state.blocked = new Set(data.items.map((item) => item.chat_id));
    renderConversationHeader();
  } catch {
    // The blocklist only decorates the header; without it the view still works.
  }
}

// ---------------------------------------------------------------------------
// Conversation: drawing
// ---------------------------------------------------------------------------

const conversationRoot = () => document.getElementById("cv-conversation");
const scroller = () => document.getElementById("cv-scroll");

/** "N messages" / "N edited" / "N deleted" line under the chat name. */
function countsLine(chat) {
  return [
    plural("cv.messagesCount", chat.message_count),
    fill(t("cv.editedCount"), { n: chat.edited_count }),
    fill(t("cv.deletedCount"), { n: chat.deleted_count }),
  ].join(" · ");
}

/** The header: avatar, name, counts, and the two actions.
 *  Redrawn alone when only the chat record or blocklist changes. */
function renderConversationHeader() {
  const head = document.getElementById("cv-head");
  const chat = state.chat;
  if (!head || !chat) return;
  const blocked = state.blocked.has(chat.chat_id);
  head.innerHTML = `
    <a class="cv-back" href="${chatRouteHash()}" aria-label="${escapeHtml(t("cv.back"))}" title="${escapeHtml(t("cv.back"))}"><span class="nav-icon" data-icon="back"></span></a>
    <span class="cv-avatar cv-avatar--lg" aria-hidden="true">${escapeHtml(initials(chat.name))}</span>
    <div class="cv-head__text">
      <h2 class="cv-head__name">${escapeHtml(chatName(chat))}</h2>
      <p class="cv-head__sub">${escapeHtml(countsLine(chat))}</p>
    </div>
    <div class="cv-head__actions">
      ${blocked ? `<span class="info-badge" id="cv-blocked-badge">${escapeHtml(t("cv.blockedBadge"))}</span>` : ""}
      <button type="button" class="cv-btn" id="cv-block" title="${escapeHtml(t("cv.block"))}" ${blocked ? "disabled" : ""}>
        <span class="nav-icon" data-icon="block"></span><span>${escapeHtml(t("cv.block"))}</span>
      </button>
      <button type="button" class="cv-btn cv-btn--danger" id="cv-purge" title="${escapeHtml(t("cv.purge"))}">
        <span class="nav-icon" data-icon="purge"></span><span>${escapeHtml(t("cv.purge"))}</span>
      </button>
    </div>`;
  renderIcons(head);
  document.getElementById("cv-block")?.addEventListener("click", onBlock);
  document.getElementById("cv-purge")?.addEventListener("click", onPurge);
}

/**
 * Versions of an edited message, newest first (the order the spec asks for: current on top, original at the bottom).
 *
 * `edits` is the archive's history oldest -> newest, each row holding the text BEFORE and AFTER one edit.
 * The original is therefore edits[0].old_text, dated by the message itself; every later version is some edit's new_text.
 *
 * @param {object} message - a MessageOut row.
 * @param {Array<object>} edits - MessageDetail.edits.
 * @returns {Array<{ text: string | null, at: string | null, kind: "current" | "original" | "edit" }>}
 */
function buildVersions(message, edits) {
  if (edits.length === 0) return [];
  const versions = [
    { text: edits[0].old_text, at: message.date, kind: "original" },
  ];
  for (const edit of edits) {
    versions.push({ text: edit.new_text, at: edit.edited_at, kind: "edit" });
  }
  versions[versions.length - 1].kind = "current";
  return versions.reverse();
}

/** The expandable panel under a message: edit history and/or the deletion note, whichever the person opened. */
function renderDetail(message) {
  const wantEdits = state.openEdits.has(message.id);
  const wantDeletion = state.openDeletion.has(message.id);
  if (!wantEdits && !wantDeletion) return "";

  const cached = state.detailCache.get(message.id);
  if (cached === undefined) {
    return `<div class="cv-detail">${escapeHtml(t("common.loading"))}</div>`;
  }
  if (cached === "error") {
    return `<div class="cv-detail cv-detail--error">${escapeHtml(t("common.error"))}</div>`;
  }

  let html = "";
  if (wantEdits) {
    const versions = buildVersions(message, cached.edits ?? []);
    const items = versions
      .map((v) => {
        const label =
          v.kind === "current"
            ? t("cv.versionCurrent")
            : v.kind === "original"
              ? t("cv.versionOriginal")
              : "";
        return `
          <li class="cv-version cv-version--${v.kind}">
            <div class="cv-version__meta">
              <time>${escapeHtml(formatDateTime(v.at))}</time>
              ${label ? `<span class="info-badge">${escapeHtml(label)}</span>` : ""}
            </div>
            <div class="cv-version__text">${v.text ? escapeHtml(v.text) : `<span class="cv-row__empty">${escapeHtml(t("messages.noText"))}</span>`}</div>
          </li>`;
      })
      .join("");
    html += `<div class="cv-detail"><ol class="cv-versions">${items || `<li>${escapeHtml(t("cv.noHistory"))}</li>`}</ol></div>`;
  }
  if (wantDeletion) {
    const deletion = cached.deletion;
    if (!deletion) {
      html += `<div class="cv-detail cv-detail--error">${escapeHtml(t("deleted.noRecord"))}</div>`;
    } else {
      const actor = deletion.deleted_by_inference;
      // The note is translated here from the inference value; the API's own `inference_confidence` is English-only (see DeletionOut).
      const note =
        actor !== "unknown"
          ? `<p class="cv-detail__note">${escapeHtml(t(`deleted.confidence.${actor}`))}</p>`
          : "";
      html += `
        <div class="cv-detail">
          <p><time>${escapeHtml(formatDateTime(deletion.deleted_at))}</time> <span class="info-badge">${escapeHtml(t(`deleted.actor.${actor}`))}</span></p>
          ${note}
        </div>`;
    }
  }
  return html;
}

/** One message bubble. */
function renderMessage(message) {
  const edits = message.edit_count ?? 0;
  const isEdited = message.is_edited || edits > 0;
  const editOpen = state.openEdits.has(message.id);
  const deletionOpen = state.openDeletion.has(message.id);

  let deletedWhen = "";
  if (message.is_deleted) {
    // Same day as the message: just the time ("Deleted 14:02"); another day: date and time, so "14:02" cannot be misread.
    deletedWhen =
      dayKey(message.deleted_at) === dayKey(message.date)
        ? formatTime(message.deleted_at)
        : formatDateTime(message.deleted_at);
  }

  const classes = [
    "cv-msg",
    message.is_deleted ? "cv-msg--deleted" : "",
    isEdited ? "cv-msg--edited" : "",
    state.anchorId === message.id ? "cv-msg--anchor" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const editedChip = isEdited
    ? `<button type="button" class="patina-badge cv-chip" data-action="edits" aria-expanded="${editOpen}">${escapeHtml(edits > 0 ? plural("cv.editedTimes", edits) : t("cv.editedUnknown"))}</button>`
    : "";
  const deletedChip = message.is_deleted
    ? `<button type="button" class="seal-badge cv-chip" data-action="deletion" aria-expanded="${deletionOpen}">${escapeHtml(fill(t("cv.deletedPreserved"), { time: deletedWhen }))}</button>`
    : "";

  return `
    <li class="${classes}" data-id="${message.id}">
      <div class="cv-msg__meta">
        <span class="cv-msg__sender">${escapeHtml(message.sender?.resolved_name ?? "")}</span>
        <time class="cv-msg__time" datetime="${escapeHtml(message.date ?? "")}">${escapeHtml(formatTime(message.date))}</time>
      </div>
      <div class="cv-msg__text">${message.text ? escapeHtml(message.text) : `<span class="cv-row__empty">${escapeHtml(t("messages.noText"))}</span>`}</div>
      ${editedChip || deletedChip ? `<div class="cv-msg__chips">${editedChip}${deletedChip}</div>` : ""}
      ${renderDetail(message)}
    </li>`;
}

/** All loaded messages with a day heading wherever the (local) day changes. */
function renderMessageList() {
  let previousDay = "";
  const parts = [];
  for (const message of state.messages) {
    const day = dayKey(message.date);
    if (day && day !== previousDay) {
      parts.push(
        `<li class="cv-day" role="presentation"><span>${escapeHtml(formatDay(message.date))}</span></li>`,
      );
      previousDay = day;
    }
    parts.push(renderMessage(message));
  }
  return parts.join("");
}

/** Draw (or redraw) the whole conversation pane from `state`. */
function renderConversation() {
  const root = conversationRoot();
  if (!root) return;
  const pane = document.getElementById("chatview");
  if (pane)
    pane.dataset.pane = state.convStatus === "none" ? "panel" : "conversation";

  if (state.convStatus === "none") {
    root.innerHTML = `<div class="cv-placeholder">${escapeHtml(t("cv.selectChat"))}</div>`;
    return;
  }
  if (state.convStatus === "loading") {
    root.innerHTML = `<div class="cv-placeholder">${escapeHtml(t("common.loading"))}</div>`;
    return;
  }
  if (state.convStatus === "error" || state.convStatus === "networkError") {
    const message =
      state.convStatus === "error"
        ? describeError(state.convError)
        : t("common.error");
    root.innerHTML = `
      <div class="cv-placeholder">
        <p>${escapeHtml(message)}</p>
        <a class="settings-fix-link" href="${chatRouteHash()}">${escapeHtml(t("cv.back"))}</a>
      </div>`;
    return;
  }

  root.innerHTML = `
    <header class="cv-head" id="cv-head"></header>
    <div class="cv-scroll" id="cv-scroll" tabindex="0">
      <p class="cv-edge" id="cv-edge-top" hidden>${escapeHtml(t("common.loading"))}</p>
      <ol class="cv-msgs" id="cv-msgs">${renderMessageList()}</ol>
      <p class="cv-edge" id="cv-edge-bottom" hidden>${escapeHtml(t("common.loading"))}</p>
    </div>
    <button type="button" class="cv-latest" id="cv-latest" hidden>
      <span class="nav-icon" data-icon="latest"></span><span>${escapeHtml(t("cv.jumpLatest"))}</span>
    </button>
    <footer class="cv-foot">${escapeHtml(t("cv.readOnly"))}</footer>`;
  renderConversationHeader();
  renderIcons(root);

  const el = scroller();
  el?.addEventListener("scroll", onScroll, { passive: true });
  el?.addEventListener("click", onMessageClick);
  document.getElementById("cv-latest")?.addEventListener("click", () => {
    location.hash = chatRouteHash(state.chat.chat_id);
  });
  updateLatestButton();
}

/** "Latest" is offered whenever newer messages exist beyond what is loaded (the view was opened on a search hit or an older window). */
function updateLatestButton() {
  const button = document.getElementById("cv-latest");
  if (button) button.hidden = !state.hasNewer;
}

/** Redraw one message in place (its chip or detail changed), keeping the scroll position and keyboard focus. */
function redrawMessage(id) {
  const message = state.messages.find((m) => m.id === id);
  const li = document.querySelector(`.cv-msg[data-id="${id}"]`);
  if (!message || !li) return;
  const focused = document.activeElement?.getAttribute?.("data-action");
  const holder = document.createElement("ol");
  holder.innerHTML = renderMessage(message);
  const fresh = holder.firstElementChild;
  if (!fresh) return;
  li.replaceWith(fresh);
  if (focused) {
    /** @type {HTMLElement | null} */ (
      fresh.querySelector(`[data-action="${focused}"]`)
    )?.focus();
  }
}

// ---------------------------------------------------------------------------
// Conversation: loading
// ---------------------------------------------------------------------------

/** Scroll to the anchor message (and outline it for a moment), or to the newest message when there is no anchor. */
function positionScroll() {
  const el = scroller();
  if (!el) return;
  const anchor =
    state.anchorId !== null
      ? el.querySelector(`.cv-msg[data-id="${state.anchorId}"]`)
      : null;
  if (anchor) {
    anchor.scrollIntoView({ block: "center" });
    clearTimeout(anchorTimer);
    anchorTimer = setTimeout(() => {
      anchor.classList.remove("cv-msg--anchor");
      state.anchorId = null;
    }, ANCHOR_HIGHLIGHT_MS);
  } else {
    el.scrollTop = el.scrollHeight;
  }
}

/**
 * Open the chat a route names.
 * Safe to call for a route that is already shown (it only re-positions).
 *
 * @param {{ chatId: number | null, messageId: number | null }} route
 */
async function openRoute(route) {
  const previous = state.route;
  state.route = route;
  renderPanelSelection();

  if (route.chatId === null) {
    state.convSeq++;
    state.chat = null;
    state.messages = [];
    state.convStatus = "none";
    renderConversation();
    return;
  }

  // Same chat, and the wanted message is already loaded: no request, just move to it.
  const sameChat = previous?.chatId === route.chatId && state.chat !== null;
  if (
    sameChat &&
    state.convStatus === "ready" &&
    (route.messageId === null
      ? !state.hasNewer
      : state.messages.some((m) => m.id === route.messageId))
  ) {
    state.anchorId = route.messageId;
    if (route.messageId !== null) {
      document
        .querySelectorAll(".cv-msg--anchor")
        .forEach((n) => n.classList.remove("cv-msg--anchor"));
      document
        .querySelector(`.cv-msg[data-id="${route.messageId}"]`)
        ?.classList.add("cv-msg--anchor");
    }
    positionScroll();
    return;
  }

  const seq = ++state.convSeq;
  state.convStatus = "loading";
  state.anchorId = null;
  renderConversation();

  const windowUrl =
    route.messageId === null
      ? `/api/chats/${route.chatId}/window`
      : `/api/chats/${route.chatId}/window?around=${route.messageId}`;
  try {
    // The window first: its errors carry a machine-readable `reason` (chat_not_found, message_not_found) the UI can translate,
    // which the plain-text 404 of GET /api/chats/{id} does not.
    const [windowRes, chatRes] = await Promise.all([
      apiFetch(windowUrl),
      apiFetch(`/api/chats/${route.chatId}`),
    ]);
    if (seq !== state.convSeq) return;
    if (!windowRes.ok || !chatRes.ok) {
      const failed = windowRes.ok ? chatRes : windowRes;
      state.convError = await readErrorDetail(failed);
      if (seq !== state.convSeq) return;
      state.convStatus = "error";
      state.chat = null;
      renderConversation();
      return;
    }
    const windowData = await windowRes.json();
    const chat = await chatRes.json();
    if (seq !== state.convSeq) return;

    state.chat = chat;
    state.messages = windowData.items;
    state.hasOlder = windowData.has_older;
    state.hasNewer = windowData.has_newer;
    state.anchorId = windowData.anchor_id;
    state.convStatus = "ready";
    renderConversation();
    positionScroll();
    fillIfShort();
  } catch {
    if (seq !== state.convSeq) return;
    state.convStatus = "networkError";
    renderConversation();
  }
}

/** Mark the open chat's row in the panel without redrawing the whole list. */
function renderPanelSelection() {
  document
    .querySelectorAll(".cv-row[aria-current]")
    .forEach((a) => a.removeAttribute("aria-current"));
  if (state.mode === "chats" && state.route?.chatId != null) {
    document
      .querySelector(`.cv-row[href="${chatRouteHash(state.route.chatId)}"]`)
      ?.setAttribute("aria-current", "true");
  }
}

/** Scrolling needs a scrollbar; when the loaded part is shorter than the pane, keep loading older messages until it is not. */
function fillIfShort() {
  const el = scroller();
  if (!el) return;
  if (el.scrollHeight <= el.clientHeight + 1) {
    if (state.hasOlder) loadOlder();
    else if (state.hasNewer) loadNewer();
  }
}

function onScroll() {
  const el = scroller();
  if (!el) return;
  if (el.scrollTop < SCROLL_LOAD_MARGIN && state.hasOlder) loadOlder();
  if (
    el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_LOAD_MARGIN &&
    state.hasNewer
  ) {
    loadNewer();
  }
}

/** Show or hide the little "Loading…" line at one end of the list. */
function setEdgeLoading(which, on) {
  const edge = document.getElementById(`cv-edge-${which}`);
  if (edge) edge.hidden = !on;
}

/** Fetch the messages just OLDER than the first loaded one and put them above, keeping what the person is reading where it is. */
async function loadOlder() {
  if (state.loadingOlder || !state.hasOlder || state.messages.length === 0)
    return;
  const seq = state.convSeq;
  const chatId = state.chat.chat_id;
  state.loadingOlder = true;
  setEdgeLoading("top", true);
  try {
    const res = await apiFetch(
      `/api/chats/${chatId}/window?before=${state.messages[0].id}`,
    );
    if (seq !== state.convSeq) return;
    if (!res.ok) {
      // Stop trying instead of hammering a failing endpoint on every scroll event; reopening the chat starts fresh.
      state.hasOlder = false;
      return;
    }
    const data = await res.json();
    if (seq !== state.convSeq) return;
    // An empty answer while has_older is true would make fillIfShort() ask again forever.
    if (data.items.length === 0) data.has_older = false;
    const el = scroller();
    const before = el ? { top: el.scrollTop, height: el.scrollHeight } : null;
    state.messages = data.items.concat(state.messages);
    state.hasOlder = data.has_older;
    const list = document.getElementById("cv-msgs");
    if (list) list.innerHTML = renderMessageList();
    // Prepending pushes the content down by exactly the height that was added; compensate so the reader's place does not jump.
    if (el && before)
      el.scrollTop = before.top + (el.scrollHeight - before.height);
  } catch {
    if (seq === state.convSeq) state.hasOlder = false;
  } finally {
    if (seq === state.convSeq) {
      state.loadingOlder = false;
      setEdgeLoading("top", false);
      fillIfShort();
    }
  }
}

/** Fetch the messages just NEWER than the last loaded one and append them. */
async function loadNewer() {
  if (state.loadingNewer || !state.hasNewer || state.messages.length === 0)
    return;
  const seq = state.convSeq;
  const chatId = state.chat.chat_id;
  state.loadingNewer = true;
  setEdgeLoading("bottom", true);
  try {
    const res = await apiFetch(
      `/api/chats/${chatId}/window?after=${state.messages[state.messages.length - 1].id}`,
    );
    if (seq !== state.convSeq) return;
    if (!res.ok) {
      state.hasNewer = false;
      updateLatestButton();
      return;
    }
    const data = await res.json();
    if (seq !== state.convSeq) return;
    if (data.items.length === 0) data.has_newer = false;
    state.messages = state.messages.concat(data.items);
    state.hasNewer = data.has_newer;
    const list = document.getElementById("cv-msgs");
    if (list) list.innerHTML = renderMessageList();
    updateLatestButton();
  } catch {
    if (seq === state.convSeq) state.hasNewer = false;
  } finally {
    if (seq === state.convSeq) {
      state.loadingNewer = false;
      setEdgeLoading("bottom", false);
      fillIfShort();
    }
  }
}

/** Click on a chip: open or close that message's edit history / deletion note, fetching the detail the first time. */
async function onMessageClick(event) {
  const button = event.target.closest?.(".cv-chip");
  if (!button) return;
  const li = button.closest(".cv-msg");
  const id = Number(li?.dataset.id);
  if (!Number.isFinite(id)) return;

  const set =
    button.dataset.action === "edits" ? state.openEdits : state.openDeletion;
  if (set.has(id)) {
    set.delete(id);
    redrawMessage(id);
    return;
  }
  set.add(id);
  redrawMessage(id);

  // Cached once it succeeded; a failure is kept as "error" only so the panel can say so, and opening it again tries again.
  const cached = state.detailCache.get(id);
  if (cached !== undefined && cached !== "error") return;
  if (cached === "error") state.detailCache.delete(id);
  redrawMessage(id);
  try {
    const res = await apiFetch(`/api/messages/${id}`);
    state.detailCache.set(id, res.ok ? await res.json() : "error");
  } catch {
    state.detailCache.set(id, "error");
  }
  redrawMessage(id); // a no-op when the message has left the screen in the meantime (another chat was opened)
}

// ---------------------------------------------------------------------------
// Actions: block this chat, delete its stored data
// ---------------------------------------------------------------------------

/** "Block this chat": stop archiving it from now on (PUT /api/ingest/blocklist/{id}).
 *  Already archived messages stay. */
async function onBlock() {
  const chat = state.chat;
  if (!chat) return;
  const confirmed = await showConfirm(t("cv.blockMessage"), {
    title: fill(t("cv.blockTitle"), { name: chatName(chat) }),
    confirmLabel: t("cv.blockConfirm"),
    danger: true,
  });
  if (!confirmed) return;

  try {
    const res = await apiFetch(`/api/ingest/blocklist/${chat.chat_id}`, {
      method: "PUT",
    });
    if (!res.ok) {
      await showAlert(describeError(await readErrorDetail(res)));
      return;
    }
    // The rule may be stored under the chat's CURRENT id (a group that became a supergroup), so trust the response.
    const rule = await res.json();
    state.blocked.add(rule.chat_id ?? chat.chat_id);
    renderConversationHeader();
  } catch {
    await showAlert(t("common.error"));
  }
}

/**
 * "Delete stored data": remove everything the archive holds for this chat (DELETE /api/chats/{id}).
 * Irreversible, so the dialog says so plainly.
 * The tick box "also stop blocking this chat" (default off, shown only when the chat IS blocked) maps to `also_unblock=true`.
 */
async function onPurge() {
  const chat = state.chat;
  if (!chat) return;
  const blocked = state.blocked.has(chat.chat_id);
  const options = {
    title: fill(t("cv.purgeTitle"), { name: chatName(chat) }),
    confirmLabel: t("cv.purgeConfirm"),
    danger: true,
  };

  let confirmed;
  let alsoUnblock = false;
  if (blocked) {
    const answer = await showConfirmWithCheckbox(t("cv.purgeMessage"), {
      ...options,
      checkboxLabel: t("cv.purgeAlsoUnblock"),
    });
    confirmed = answer.confirmed;
    alsoUnblock = answer.checked;
  } else {
    confirmed = await showConfirm(t("cv.purgeMessage"), options);
  }
  if (!confirmed) return;

  try {
    const res = await apiFetch(
      `/api/chats/${chat.chat_id}${alsoUnblock ? "?also_unblock=true" : ""}`,
      { method: "DELETE" },
    );
    if (!res.ok) {
      // 409 backfill_running and 404 chat_not_found both have translated texts (lib/errors.js).
      await showAlert(describeError(await readErrorDetail(res)));
      return;
    }
    const result = await res.json();
    if (alsoUnblock) state.blocked.delete(chat.chat_id);

    // The chat is gone: leave it, and rebuild the panel so it disappears from every list.
    state.detailCache.clear();
    state.openEdits.clear();
    state.openDeletion.clear();
    location.hash = chatRouteHash();
    openRoute({ chatId: null, messageId: null }); // the hashchange event does the same; this covers a hash that was already "#/chats"
    loadPanelPage({ reset: true });

    const lines = [
      fill(t("cv.purgeDone"), {
        messages: result.messages_deleted,
        edits: result.edits_deleted,
      }),
    ];
    if (result.block_rule_kept) lines.push(t("cv.purgeRuleKept"));
    await showAlert(lines.join("\n"));
  } catch {
    await showAlert(t("common.error"));
  }
}

// ---------------------------------------------------------------------------
// Entry points (called by app.js)
// ---------------------------------------------------------------------------

/** Read the current hash and show what it names.
 *  A hash that is not a chat route leaves the screen as it is. */
function syncFromHash() {
  const route = parseChatRoute(location.hash);
  if (route) openRoute(route);
}

/**
 * Called by app.js every time the Chats tab is switched back to (not on first load).
 * Quietly brings the visible data up to date so what the live archiver stored in the meantime shows up without a reload:
 * the panel's list is re-fetched, and - when the person is reading the end of a chat - the newest window is merged into what is loaded
 * (new messages appended; messages that became deleted or edited updated in place).
 * Older loaded messages are not re-checked.
 */
async function refreshChatView() {
  refetchPanel();

  if (state.convStatus === "ready" && state.chat && !state.hasNewer) {
    const seq = state.convSeq;
    try {
      const [windowRes, chatRes] = await Promise.all([
        apiFetch(`/api/chats/${state.chat.chat_id}/window`),
        apiFetch(`/api/chats/${state.chat.chat_id}`),
      ]);
      if (seq !== state.convSeq || !windowRes.ok) return;
      const data = await windowRes.json();
      if (chatRes.ok) state.chat = await chatRes.json();
      mergeNewest(data.items);
    } catch {
      // Keep what is shown.
    }
  }
}

/** Fold a freshly fetched newest window into the loaded messages.
 *  See refreshChatView(). */
function mergeNewest(fresh) {
  const freshById = new Map(fresh.map((m) => [m.id, m]));
  let changed = false;
  state.messages = state.messages.map((m) => {
    const next = freshById.get(m.id);
    if (!next) return m;
    if (next.is_deleted !== m.is_deleted || next.edit_count !== m.edit_count) {
      state.detailCache.delete(m.id); // its history or deletion record is out of date now
      changed = true;
    }
    return next;
  });
  const known = new Set(state.messages.map((m) => m.id));
  const added = fresh.filter((m) => !known.has(m.id));
  if (added.length > 0) {
    state.messages = state.messages.concat(added);
    changed = true;
  }
  renderConversationHeader();
  if (!changed) return;

  const el = scroller();
  const atBottom = el
    ? el.scrollHeight - el.scrollTop - el.clientHeight < 40
    : false;
  const list = document.getElementById("cv-msgs");
  if (list) list.innerHTML = renderMessageList();
  if (el && atBottom) el.scrollTop = el.scrollHeight;
}

/** Wire the panel's controls, draw the empty states, and open whatever the URL already names.
 *  Idempotent. */
let initialized = false;
function initChatView() {
  if (initialized) return;
  initialized = true;

  document.querySelectorAll(".cv-switch__btn").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  const search = document.getElementById("cv-search");
  search?.addEventListener("input", () => {
    state.q = /** @type {HTMLInputElement} */ (search).value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      // In chats mode the search filters what is already loaded; only the pages still missing need fetching.
      if (state.mode === "chats") {
        renderPanel();
        if (state.q.trim() && state.chats.page < state.chats.pages)
          loadPanelPage();
      } else {
        loadPanelPage({ reset: true });
      }
    }, SEARCH_DEBOUNCE_MS);
  });

  renderSearchPlaceholder();
  renderConversation();
  loadPanelPage({ reset: true });
  loadBlocklist();

  window.addEventListener("hashchange", syncFromHash);
  syncFromHash();

  // Labels built from data (rows, chips, header) are redrawn from state in the new language; no request is needed.
  document.addEventListener("televault:langchange", () => {
    renderSearchPlaceholder();
    renderPanel();
    if (state.convStatus === "ready") {
      const el = scroller();
      const top = el?.scrollTop ?? 0;
      renderConversation();
      if (scroller()) scroller().scrollTop = top;
    } else {
      renderConversation();
    }
  });
}

export { initChatView, refreshChatView };
