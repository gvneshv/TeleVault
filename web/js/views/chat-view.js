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
 * Narrowing and moving around a conversation (step D)
 *   - The All / Edited / Deleted switch and the in-chat search both narrow the SAME window request (`kind=`, `q=`),
 *     so a narrowed conversation is a list of matches in order, scrolled and paged exactly like the full one.
 *     The text being searched for lives only in memory and in that request's query string - never in the address bar (a route is ids only).
 *     Each bubble of a narrowed view offers "Show in context", a normal route that clears the narrowing.
 *   - Jump to date asks for `around_date=<start of that local day>`; the window lands on the first message at or after it (on the first MATCHING one while narrowed).
 *     The route is then rewritten to the id that was found (history.replaceState, so the Back button does not fill with date jumps).
 *   - The panel can be dragged wider or narrower (width remembered per browser).
 *     Below 1100px it becomes an overlay opened from the header, below 640px the view shows one pane at a time with a back arrow.
 *
 * What is deliberately not here: the old pages' "whole word" search and sort order, and a "N matches" counter (the window API has no count).
 *
 * Rendering style follows the other views: markup is built as strings, EVERYTHING that comes from Telegram or the archive goes through escapeHtml(),
 * and the last fetched state is kept so a language change redraws without a request.
 * Message detail (edit history, deletion record) is fetched lazily on first click from GET /api/messages/{id} and cached for the page's lifetime:
 * a stored edit history only ever grows, and the cache entry is dropped when a refresh sees the message change.
 *
 * Display names (Phase 3)
 *   The owner can give a sender their own label ("Mum"): GET /api/senders/display-names is fetched once and kept in `state.displayNames`,
 *   and senderName() puts it in front of Telegram's name in the bubbles and in the panel's message rows.
 *   The message endpoints are not involved, so paging, search (which matches Telegram's names and the text, not labels) and the live refresh behave exactly as before.
 *   Clicking a sender's name opens a dialog to rename them (PUT/DELETE /api/senders/{id}/display-name) and lists the names TeleVault has seen them under.
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
  chatDisplayName,
  fetchDisplayNames,
  isPrivateChat,
} from "../lib/display-names.js";
import {
  showAlert,
  showConfirm,
  showConfirmWithCheckbox,
  showFieldDialog,
} from "../lib/dialog.js";
import { parseChatRoute, chatRouteHash } from "../lib/router.js";

const CHATS_PER_PAGE = 100;
const RESULTS_PER_PAGE = 30;
const SEARCH_DEBOUNCE_MS = 300;
// Start fetching the next batch when the scroll position is this close to the end of what is loaded (px).
const SCROLL_LOAD_MARGIN = 300;
// How long the message a search hit opened is outlined.
// How long the jumped-to message stays highlighted.
// Must match the 6 s `cv-anchor-wash` animation in css/base.css (three slow pulses, then fade-out).
const ANCHOR_HIGHLIGHT_MS = 6000;
// Mirrors DISPLAY_NAME_MAX_LENGTH in utils/validation.py (the server is the one that enforces it).
const DISPLAY_NAME_MAX_LENGTH = 64;
// Mirrors SEARCH_QUERY_MAX_LENGTH in utils/validation.py (also the maxlength of the panel's search box in index.html).
const CHAT_SEARCH_MAX_LENGTH = 200;
// The chats panel's width: bounds, default, and where the person's choice is remembered.
// The CSS default in base.css repeats DEFAULT.
const PANEL_MIN_WIDTH = 240;
const PANEL_MAX_WIDTH = 560;
const PANEL_DEFAULT_WIDTH = 320;
const PANEL_WIDTH_STORAGE_KEY = "televault.chatview.panelWidth";
// Keep at least this much for the conversation when the panel is dragged wide.
const CONVERSATION_MIN_WIDTH = 360;

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

  /** sender id -> the owner's own label for that sender (GET /api/senders/display-names).
   *  Empty until loaded or on an archive that has none. */
  displayNames: new Map(),

  /** Telegram user id of the archive's owner (GET /api/archive/owner), or null while unknown: their messages are drawn on the right. */
  ownerId: null,

  /** message id -> MessageDetail from GET /api/messages/{id}, or "error". */
  detailCache: new Map(),
  /** message ids whose edit-history / deletion note is open. */
  openEdits: new Set(),
  openDeletion: new Set(),

  /** The conversation's narrowing: "all" | "edited" | "deleted", and the in-chat search text.
   *  Reset whenever another chat or a message in context is opened. */
  kind: "all",
  cq: "",
  /** Set when reloading the narrowed list failed, so an empty list is not mistaken for "nothing matches". */
  listError: false,
  /** Overlay layout (tablet): the chats panel is shown over the conversation. */
  panelOpen: false,
};

let searchTimer = null;
let anchorTimer = null;
let chatSearchTimer = null;

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

/** True while the conversation is narrowed by the kind switch or the in-chat search. */
function filtersActive() {
  return state.kind !== "all" || state.cq.trim() !== "";
}

function resetFilters() {
  state.kind = "all";
  state.cq = "";
  state.listError = false;
}

/**
 * The URL of one window request:
 * the anchor / cursor parameters, plus the active narrowing, so every read of a conversation (first page, older, newer, date jump) is narrowed the same way.
 *
 * @param {number} chatId
 * @param {Record<string, string | number>} [extra] e.g. { before: 12 } or { around_date: "2026-10-31T22:00:00.000Z" }
 */
function windowUrl(chatId, extra = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(extra))
    params.set(key, String(value));
  if (state.kind !== "all") params.set("kind", state.kind);
  if (state.cq.trim()) params.set("q", state.cq.trim());
  const query = params.toString();
  return `/api/chats/${chatId}/window${query ? `?${query}` : ""}`;
}

/** @param {object} chat @returns {string} the chat's name, or its id for a chat Telegram never named. */
function chatName(chat) {
  // A private chat takes the owner's label for that person (lib/display-names.js), so three friends with the same names can be told apart.
  return chatDisplayName(
    state.displayNames,
    chat,
    chat?.name || String(chat?.chat_id ?? ""),
  );
}

/**
 * What to call a sender: the owner's own label if there is one, otherwise the name Telegram gave them (the API's resolved_name).
 * A message with no sender (a channel post) has no name at all.
 */
function senderName(sender) {
  if (!sender) return "";
  return state.displayNames.get(sender.sender_id) ?? sender.resolved_name ?? "";
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
        <span class="cv-avatar" aria-hidden="true">${escapeHtml(initials(chatName(chat)))}</span>
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
  const sender = senderName(message.sender);
  const chip =
    state.mode === "messages" && message.is_deleted
      ? `<span class="seal-badge">${escapeHtml(t("cv.deletedShort"))}</span>`
      : "";
  return `
    <li>
      <a class="cv-row cv-row--result" href="${chatRouteHash(message.chat?.chat_id ?? null, message.id)}">
        <span class="cv-avatar" aria-hidden="true">${escapeHtml(initials(chatName(message.chat)))}</span>
        <span class="cv-row__body">
          <span class="cv-row__top">
            <span class="cv-row__name">${escapeHtml(chatName(message.chat))}</span>
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
  document
    .querySelectorAll(".chatview__panel-head .cv-switch__btn")
    .forEach((btn) => {
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

/**
 * Load the owner's display names (once at start; changes made here update the map directly).
 * An archive that has not been migrated answers an empty list, and a failure simply leaves Telegram's names in place.
 * If a conversation is already on screen, its messages and the panel rows are redrawn with the labels.
 */
async function loadDisplayNames() {
  state.displayNames = await fetchDisplayNames();
  redrawSenderNames();
}

/** Redraw everything that shows a sender's name, after a label changed.
 *  Keeps the scroll position (messages are replaced one by one). */
function redrawSenderNames() {
  if (state.convStatus === "ready")
    state.messages.forEach((m) => redrawMessage(m.id));
  renderPanel();
  renderConversationHeader();
}

/** One line for a name-history entry: "@username · First Last", with whatever is known. */
function formatNameVersion(version) {
  const full = [version.first_name, version.last_name]
    .filter(Boolean)
    .join(" ");
  const parts = [full, version.username ? `@${version.username}` : ""].filter(
    Boolean,
  );
  return parts.join(" · ") || t("sender.noName");
}

/** GET /api/senders/{id}, or null after telling the person why not (a sender the archive has never seen is a 404). */
async function fetchSenderDetail(senderId) {
  if (!Number.isFinite(senderId)) return null;
  try {
    const res = await apiFetch(`/api/senders/${senderId}`);
    if (res.ok) return await res.json();
    await showAlert(describeError(await readErrorDetail(res)));
  } catch {
    await showAlert(t("common.error"));
  }
  return null;
}

/** The "names TeleVault has seen" block: newest first, ending with the identity the sender was first seen under. @returns {HTMLElement} */
function buildNameHistory(detail) {
  const history = document.createElement("div");
  history.className = "modal__history";
  const heading = document.createElement("p");
  heading.className = "modal__history-title";
  heading.textContent = t("sender.historyTitle");
  const list = document.createElement("ul");
  detail.history.forEach((version, index) => {
    const item = document.createElement("li");
    item.textContent = formatNameVersion(version);
    if (version.seen_at) {
      const when = document.createElement("time");
      when.dateTime = version.seen_at;
      // The oldest entry is the identity the sender was first seen under, the others are later sightings.
      when.textContent = ` - ${fill(
        index === detail.history.length - 1
          ? t("sender.firstSeen")
          : t("sender.seenOn"),
        { date: formatDateTime(version.seen_at) },
      )}`;
      item.append(when);
    }
    list.append(item);
  });
  history.append(heading, list);
  if (detail.history.length === 1) {
    const note = document.createElement("p");
    note.className = "modal__history-note";
    note.textContent = t("sender.noChanges");
    history.append(note);
  }
  return history;
}

/** The chat header's history button: a read-only list of the names this person has been seen under (a private chat's id is the person's id). */
async function openNameHistory(senderId) {
  const detail = await fetchSenderDetail(senderId);
  if (!detail) return;
  await showAlert(t("sender.historyMessage"), {
    title: fill(t("sender.historyDialogTitle"), {
      name: chatDisplayName(
        state.displayNames,
        { chat_id: senderId, chat_type: "private" },
        formatNameVersion(detail.history[0]),
      ),
    }),
    content: buildNameHistory(detail),
  });
}

/**
 * The rename dialog for one sender: their current label, and under it the names TeleVault has seen them under.
 * "Seen" is deliberate: the archiver learns of a change when the person writes next (or a backfill reads them), not when they made it.
 */
async function openSenderDialog(senderId, { showHistory = true } = {}) {
  const detail = await fetchSenderDetail(senderId);
  if (!detail) return;

  const current = state.displayNames.get(senderId) ?? detail.display_name ?? "";
  const answer = await showFieldDialog(t("sender.message"), {
    title: fill(t("sender.title"), {
      name: formatNameVersion(detail.history[0]),
    }),
    confirmLabel: t("sender.save"),
    fieldLabel: t("sender.fieldLabel"),
    value: current,
    maxLength: DISPLAY_NAME_MAX_LENGTH,
    blankMessage: current ? t("sender.blank") : t("sender.blankNew"),
    secondaryLabel: current ? t("sender.remove") : undefined,
    secondaryDanger: true,
    content: showHistory ? buildNameHistory(detail) : undefined,
  });
  if (answer.action === "cancel") return;
  if (answer.action === "confirm" && answer.value === current) return;

  try {
    const res = await apiFetch(`/api/senders/${senderId}/display-name`, {
      method: answer.action === "secondary" ? "DELETE" : "PUT",
      ...(answer.action === "confirm"
        ? {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ display_name: answer.value }),
          }
        : {}),
    });
    if (!res.ok) {
      await showAlert(describeError(await readErrorDetail(res)));
      return;
    }
    if (answer.action === "secondary") state.displayNames.delete(senderId);
    else state.displayNames.set(senderId, answer.value);
    redrawSenderNames();
  } catch {
    await showAlert(t("common.error"));
  }
}

/**
 * Fetch who the archive's owner is, so their own messages can be drawn on the right.
 * Unknown (an archive not yet updated, or an archiver that has not recorded it) simply leaves every message on the left - same as before.
 * If the conversation is already on screen when the answer arrives, its messages are redrawn one by one (no scroll jump).
 */
async function loadOwner() {
  try {
    const res = await apiFetch("/api/archive/owner");
    if (!res.ok) return;
    const data = await res.json();
    const ownerId = typeof data.owner_id === "number" ? data.owner_id : null;
    if (ownerId === state.ownerId) return;
    state.ownerId = ownerId;
    if (state.convStatus === "ready")
      state.messages.forEach((m) => redrawMessage(m.id));
  } catch {
    // Decoration only; the conversation reads fine without it.
  }
}

/** True when the archive's owner wrote this message.
 *  Channel posts have no sender, so they are never "own". */
function isOwnMessage(message) {
  return (
    state.ownerId !== null &&
    message.sender?.sender_id !== undefined &&
    message.sender?.sender_id === state.ownerId
  );
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
    <button type="button" class="cv-btn cv-btn--icon cv-panel-toggle" id="cv-panel-toggle" aria-controls="cv-panel" aria-expanded="${state.panelOpen}"
      aria-label="${escapeHtml(t("cv.panelToggle"))}" title="${escapeHtml(t("cv.panelToggle"))}"><span class="nav-icon" data-icon="panel"></span></button>
    <a class="cv-back" href="${chatRouteHash()}" aria-label="${escapeHtml(t("cv.back"))}" title="${escapeHtml(t("cv.back"))}"><span class="nav-icon" data-icon="back"></span></a>
    <span class="cv-avatar cv-avatar--lg" aria-hidden="true">${escapeHtml(initials(chatName(chat)))}</span>
    <div class="cv-head__text">
      <h2 class="cv-head__name">${escapeHtml(chatName(chat))}${
        isPrivateChat(chat)
          ? `
        <button type="button" class="cv-head__tool" id="cv-rename" title="${escapeHtml(t("sender.renameChat"))}" aria-label="${escapeHtml(t("sender.renameChat"))}"><span class="nav-icon" data-icon="pencil"></span></button>
        <button type="button" class="cv-head__tool" id="cv-name-history" title="${escapeHtml(t("sender.historyOpen"))}" aria-label="${escapeHtml(t("sender.historyOpen"))}"><span class="nav-icon" data-icon="history"></span></button>`
          : ""
      }</h2>
      <p class="cv-head__sub">${escapeHtml(countsLine(chat))}</p>
    </div>
    <div class="cv-head__actions">
      ${blocked ? `<span class="info-badge" id="cv-blocked-badge">${escapeHtml(t("cv.blockedBadge"))}</span>` : ""}
      <button type="button" class="cv-btn" id="cv-block" title="${escapeHtml(t(blocked ? "cv.unblock" : "cv.block"))}">
        <span class="nav-icon" data-icon="${blocked ? "unblock" : "block"}"></span><span>${escapeHtml(t(blocked ? "cv.unblock" : "cv.block"))}</span>
      </button>
      <button type="button" class="cv-btn cv-btn--danger" id="cv-purge" title="${escapeHtml(t("cv.purge"))}">
        <span class="nav-icon" data-icon="purge"></span><span>${escapeHtml(t("cv.purge"))}</span>
      </button>
    </div>`;
  renderIcons(head);
  document
    .getElementById("cv-rename")
    ?.addEventListener("click", () =>
      openSenderDialog(chat.chat_id, { showHistory: false }),
    );
  document
    .getElementById("cv-name-history")
    ?.addEventListener("click", () => openNameHistory(chat.chat_id));
  document.getElementById("cv-panel-toggle")?.addEventListener("click", () => {
    setPanelOpen(!state.panelOpen);
    if (state.panelOpen) document.getElementById("cv-search")?.focus();
  });
  document.getElementById("cv-block")?.addEventListener("click", onToggleBlock);
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
// Lucide "pencil", inline because message HTML is built as a string (the nav icons are filled in afterwards by renderIcons).
// Decorative: the button has its own label.
const PENCIL_SVG =
  '<svg class="cv-msg__edit-icon" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>';

/** The name above a bubble.
 *  Someone else's name is a button that opens the rename dialog; "You" and a channel post have nothing to rename. */
function renderSenderName(message) {
  if (isOwnMessage(message))
    return `<span class="cv-msg__sender">${escapeHtml(t("cv.you"))}</span>`;
  const name = senderName(message.sender);
  if (!name || message.sender?.sender_id === undefined)
    return `<span class="cv-msg__sender">${escapeHtml(name)}</span>`;
  return `<button type="button" class="cv-msg__sender cv-msg__sender--btn" data-action="sender" data-sender-id="${message.sender.sender_id}" title="${escapeHtml(t("sender.open"))}" aria-label="${escapeHtml(`${name} - ${t("sender.open")}`)}">${escapeHtml(name)}${PENCIL_SVG}</button>`;
}

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
    isOwnMessage(message) ? "cv-msg--own" : "",
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

  // A narrowed list shows matches without their surroundings; this is the way back to them (a normal route, which also clears the narrowing).
  const contextLink =
    filtersActive() && state.chat
      ? `<a class="cv-context" href="${chatRouteHash(state.chat.chat_id, message.id)}">${escapeHtml(t("cv.showInContext"))}</a>`
      : "";

  return `
    <li class="${classes}" data-id="${message.id}">
      <div class="cv-msg__meta">
        ${renderSenderName(message)}
        <time class="cv-msg__time" datetime="${escapeHtml(message.date ?? "")}">${escapeHtml(formatTime(message.date))}</time>
      </div>
      <div class="cv-msg__text">${message.text ? (state.cq.trim() ? highlightMatches(escapeHtml(message.text), state.cq.trim()) : escapeHtml(message.text)) : `<span class="cv-row__empty">${escapeHtml(t("messages.noText"))}</span>`}</div>
      ${editedChip || deletedChip || contextLink ? `<div class="cv-msg__chips">${editedChip}${deletedChip}${contextLink}</div>` : ""}
      ${renderDetail(message)}
    </li>`;
}

/**
 * Internal id of the message the "Unread messages" bar goes right before, or null for no bar.
 *
 * The bar mirrors the owner's reading position IN TELEGRAM (chat.read_inbox_max_id: every message with a Telegram id up to it is read).
 * It marks the first message the owner has not read AND did not write themselves (their own messages are never "unread").
 * It is found by walking back from the newest loaded message to the first read one, so only the unbroken run of unread messages at the end counts.
 * That keeps it right in a chat that was once a group and became a supergroup, whose older messages carry ids of another numbering.
 *
 * No bar - on purpose, not by accident - when:
 *   - the position is unknown (null: not reported yet, or an archive that has not been migrated),
 *   - the owner's id is unknown (their own messages cannot be told apart, so the bar could sit before one of them),
 *   - the list is narrowed to edited/deleted messages or a search (a bar in the middle of a filtered list would mean nothing),
 *   - every loaded message is unread but older ones exist (the real boundary is further up, in a page that is not loaded yet),
 *   - nothing unread was written by anybody else.
 * Display only: nothing here, or anywhere in TeleVault, marks a message as read in Telegram.
 */
function findUnreadStartId() {
  const position = state.chat?.read_inbox_max_id;
  if (typeof position !== "number" || state.ownerId === null) return null;
  if (filtersActive()) return null;

  let start = null;
  let reachedRead = false;
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const message = state.messages[i];
    if (message.tg_message_id <= position) {
      reachedRead = true;
      break;
    }
    if (!isOwnMessage(message)) start = message.id;
  }
  if (!reachedRead && state.hasOlder) return null;
  return start;
}

/** The bar itself: a full-width line with a label, as a list item between messages. */
function renderUnreadBar() {
  return `<li class="cv-unread" id="cv-unread" role="separator" aria-label="${escapeHtml(t("cv.unreadBar"))}"><span>${escapeHtml(t("cv.unreadBar"))}</span></li>`;
}

/**
 * Move, add or remove the bar WITHOUT redrawing the messages - for when only the reading position changed
 * (the owner read the chat on their phone; the live refresh noticed).
 * A full redraw would be harmless but could jump the scroll position.
 */
function syncUnreadBar() {
  document.getElementById("cv-unread")?.remove();
  const id = findUnreadStartId();
  if (id === null) return;
  document
    .querySelector(`.cv-msg[data-id="${id}"]`)
    ?.insertAdjacentHTML("beforebegin", renderUnreadBar());
}

/** All loaded messages with a day heading wherever the (local) day changes. */
function renderMessageList() {
  if (state.messages.length === 0) {
    const key = state.listError
      ? "common.error"
      : filtersActive()
        ? "cv.noMatch"
        : "cv.emptyChat";
    return `<li class="cv-empty">${escapeHtml(t(key))}</li>`;
  }
  let previousDay = "";
  const parts = [];
  const unreadStartId = findUnreadStartId();
  for (const message of state.messages) {
    const day = dayKey(message.date);
    if (day && day !== previousDay) {
      parts.push(
        `<li class="cv-day" role="presentation"><span>${escapeHtml(formatDay(message.date))}</span></li>`,
      );
      previousDay = day;
    }
    // After the day heading: when the first unread message starts a day, the bar sits between the heading and it, as in Telegram.
    if (message.id === unreadStartId) parts.push(renderUnreadBar());
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
    <div class="cv-tools" id="cv-tools">
      <div class="cv-switch cv-switch--kind" role="group" aria-label="${escapeHtml(t("cv.filterLabel"))}">
        ${["all", "edited", "deleted"]
          .map(
            (kind) =>
              `<button type="button" class="cv-switch__btn" data-kind="${kind}" aria-pressed="${state.kind === kind}">${escapeHtml(t(`cv.kind.${kind}`))}</button>`,
          )
          .join("")}
      </div>
      <label class="cv-search cv-search--chat">
        <span class="nav-icon" data-icon="search"></span>
        <input id="cv-chat-search" type="search" maxlength="${CHAT_SEARCH_MAX_LENGTH}" autocomplete="off" spellcheck="false"
          value="${escapeHtml(state.cq)}" placeholder="${escapeHtml(t("cv.chatSearchPlaceholder"))}" aria-label="${escapeHtml(t("cv.chatSearchLabel"))}" />
      </label>
      <span class="cv-date">
        <button type="button" class="cv-date__btn" id="cv-date-btn" title="${escapeHtml(t("cv.dateLabel"))}" aria-label="${escapeHtml(t("cv.dateLabel"))}">
          <span class="nav-icon" data-icon="calendar"></span>
        </button>
        <!-- The real field is invisible; the button above opens its picker (see css/base.css, .cv-date). Not reachable on its own: the button is the control. -->
        <input id="cv-date" class="cv-date__input" type="date" tabindex="-1" aria-hidden="true" />
      </span>
    </div>
    <div class="cv-banner" id="cv-banner" role="status" hidden></div>
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

  document.querySelectorAll("#cv-tools [data-kind]").forEach((btn) => {
    btn.addEventListener("click", () =>
      setKind(/** @type {HTMLElement} */ (btn).dataset.kind),
    );
  });
  document
    .getElementById("cv-chat-search")
    ?.addEventListener("input", onChatSearchInput);
  document.getElementById("cv-date")?.addEventListener("change", onDateChange);
  document
    .getElementById("cv-date-btn")
    ?.addEventListener("click", openDatePicker);
  updateBanner();

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
// Conversation: narrowing and jumping
// ---------------------------------------------------------------------------

/** Show or hide the line that says the list is narrowed, with its way out. */
function updateBanner() {
  const banner = document.getElementById("cv-banner");
  if (!banner) return;
  if (!filtersActive()) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  const hasText = state.cq.trim() !== "";
  const key =
    state.kind === "all"
      ? "cv.filterQuery"
      : `cv.filter${state.kind === "edited" ? "Edited" : "Deleted"}${hasText ? "Query" : ""}`;
  banner.innerHTML = `<span>${escapeHtml(fill(t(key), { q: state.cq.trim() }))}</span>
    <button type="button" class="cv-banner__clear">${escapeHtml(t("cv.clearFilter"))}</button>`;
  banner
    .querySelector(".cv-banner__clear")
    ?.addEventListener("click", clearFilters);
  banner.hidden = false;
}

/** Reflect `state.kind` on the switch's buttons. */
function syncKindButtons() {
  document.querySelectorAll("#cv-tools [data-kind]").forEach((btn) => {
    btn.setAttribute(
      "aria-pressed",
      String(/** @type {HTMLElement} */ (btn).dataset.kind === state.kind),
    );
  });
}

function setKind(kind) {
  if (!kind || kind === state.kind) return;
  state.kind = kind;
  syncKindButtons();
  reloadConversation();
}

function onChatSearchInput(event) {
  state.cq = event.target.value;
  clearTimeout(chatSearchTimer);
  chatSearchTimer = setTimeout(reloadConversation, SEARCH_DEBOUNCE_MS);
}

/** "Show all messages": drop the narrowing and go back to the newest end. */
function clearFilters() {
  resetFilters();
  const input = /** @type {HTMLInputElement | null} */ (
    document.getElementById("cv-chat-search")
  );
  if (input) input.value = "";
  syncKindButtons();
  reloadConversation();
}

/**
 * Re-read the open chat from its newest end with the current narrowing, in place:
 * the header, the tools row and the typed text stay where they are, only the list is replaced.
 * A late answer for a state the person has already moved on from is dropped (convSeq).
 */
async function reloadConversation() {
  const chat = state.chat;
  if (!chat || state.convStatus !== "ready") return;
  const seq = ++state.convSeq;
  state.loadingOlder = false;
  state.loadingNewer = false;
  setEdgeLoading("top", false);
  setEdgeLoading("bottom", false);
  updateBanner();
  try {
    const res = await apiFetch(windowUrl(chat.chat_id));
    if (seq !== state.convSeq) return;
    if (!res.ok) throw new Error(`window ${res.status}`);
    const data = await res.json();
    if (seq !== state.convSeq) return;
    state.messages = data.items;
    state.hasOlder = data.has_older;
    state.hasNewer = data.has_newer;
    state.anchorId = null;
    state.listError = false;
  } catch {
    if (seq !== state.convSeq) return;
    state.messages = [];
    state.hasOlder = false;
    state.hasNewer = false;
    state.listError = true;
  }
  const list = document.getElementById("cv-msgs");
  if (list) list.innerHTML = renderMessageList();
  updateLatestButton();
  positionScroll();
  fillIfShort();
}

/**
 * Jump to a calendar date picked in the header.
 * The date is the START OF THAT DAY IN THE PERSON'S OWN TIME ZONE, sent as an instant;
 * a bare "2026-10-31" would mean midnight UTC to the API and could land hours off the day that was meant.
 * The window then centres on the first message at or after it (the first matching one while the list is narrowed).
 */
/**
 * Open the browser's date picker for the (invisible) date field.
 * showPicker() is the standard way; it can throw (older browsers, or a page the browser considers inactive), and then focusing and clicking the field is the fallback.
 */
function openDatePicker() {
  const input = /** @type {HTMLInputElement | null} */ (
    document.getElementById("cv-date")
  );
  if (!input) return;
  try {
    input.showPicker();
  } catch {
    input.focus();
    input.click();
  }
}

async function onDateChange(event) {
  const chat = state.chat;
  const value = event.target.value; // "YYYY-MM-DD", or "" when the field was cleared
  // Emptied right away so choosing the SAME day again later still fires a change event (a field that keeps its value would stay silent).
  event.target.value = "";
  if (!chat || !value) return;
  const [year, month, day] = value.split("-").map(Number);
  const start = new Date(year, month - 1, day);
  if (Number.isNaN(start.getTime())) return;

  const seq = ++state.convSeq;
  state.loadingOlder = false;
  state.loadingNewer = false;
  try {
    const res = await apiFetch(
      windowUrl(chat.chat_id, { around_date: start.toISOString() }),
    );
    if (seq !== state.convSeq) return;
    if (!res.ok) {
      await showAlert(describeError(await readErrorDetail(res)));
      return;
    }
    const data = await res.json();
    if (seq !== state.convSeq) return;
    state.messages = data.items;
    state.hasOlder = data.has_older;
    state.hasNewer = data.has_newer;
    state.anchorId = data.anchor_id;
    state.listError = false;
    const list = document.getElementById("cv-msgs");
    if (list) list.innerHTML = renderMessageList();
    updateLatestButton();
    // Make the address say where we are (ids only) without a history entry per jump, and without openRoute() running again for it.
    if (data.anchor_id !== null) {
      state.route = { chatId: chat.chat_id, messageId: data.anchor_id };
      history.replaceState(
        null,
        "",
        chatRouteHash(chat.chat_id, data.anchor_id),
      );
    }
    positionScroll();
    fillIfShort();
  } catch {
    if (seq === state.convSeq) await showAlert(t("common.error"));
  }
}

// ---------------------------------------------------------------------------
// Panel: overlay layout and resizing
// ---------------------------------------------------------------------------

/** Show or hide the chats panel over the conversation (only has a visible effect in the overlay layout, see base.css). */
function setPanelOpen(open) {
  state.panelOpen = open;
  const view = document.getElementById("chatview");
  if (view) view.dataset.overlay = open ? "open" : "closed";
  document
    .getElementById("cv-panel-toggle")
    ?.setAttribute("aria-expanded", String(open));
}

/** Drag the panel's right edge, or use the arrow keys on it.
 *  The width is remembered per browser (localStorage, which may be unavailable). */
function initPanelResizer() {
  const handle = document.getElementById("cv-resizer");
  const view = document.getElementById("chatview");
  if (!handle || !view) return;

  const clamp = (px) => {
    // While the Chats tab is hidden (the saved width is applied at start-up, before the tab is shown) the view has no width yet;
    // clamping against 0 would shrink every saved width to the minimum.
    // base.css limits the column to 60% of the view anyway.
    const room =
      view.clientWidth > 0
        ? Math.max(view.clientWidth - CONVERSATION_MIN_WIDTH, PANEL_MIN_WIDTH)
        : PANEL_MAX_WIDTH;
    return Math.round(
      Math.min(Math.max(px, PANEL_MIN_WIDTH), Math.min(PANEL_MAX_WIDTH, room)),
    );
  };
  const apply = (px) => {
    const width = clamp(px);
    view.style.setProperty("--cv-panel-width", `${width}px`);
    handle.setAttribute("aria-valuenow", String(width));
    return width;
  };
  const remember = () => {
    try {
      localStorage.setItem(
        PANEL_WIDTH_STORAGE_KEY,
        handle.getAttribute("aria-valuenow") ?? "",
      );
    } catch {
      // Private mode or blocked storage: the width just resets next time.
    }
  };

  try {
    const saved = Number(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY));
    if (Number.isFinite(saved) && saved > 0) apply(saved);
  } catch {
    // Same as above: use the default.
  }
  handle.setAttribute("aria-valuemin", String(PANEL_MIN_WIDTH));
  handle.setAttribute("aria-valuemax", String(PANEL_MAX_WIDTH));
  if (!handle.hasAttribute("aria-valuenow"))
    handle.setAttribute("aria-valuenow", String(PANEL_DEFAULT_WIDTH));

  let dragging = false;
  handle.addEventListener("pointerdown", (event) => {
    dragging = true;
    handle.setPointerCapture(event.pointerId);
    view.classList.add("is-resizing"); // no text selection while dragging (CSS)
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (dragging) apply(event.clientX - view.getBoundingClientRect().left);
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    view.classList.remove("is-resizing");
    remember();
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("keydown", (event) => {
    const current =
      Number(handle.getAttribute("aria-valuenow")) || PANEL_DEFAULT_WIDTH;
    const next = {
      ArrowLeft: current - 16,
      ArrowRight: current + 16,
      Home: PANEL_MIN_WIDTH,
      End: PANEL_MAX_WIDTH,
    }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    apply(next);
    remember();
  });
  handle.addEventListener("dblclick", () => {
    apply(PANEL_DEFAULT_WIDTH);
    remember();
  });
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
    resetFilters();
    renderConversation();
    return;
  }

  // Another chat, or a message "in context", starts without the narrowing: it belonged to the list the person came from.
  // If a narrowing WAS in place, what is loaded is only its matches, so the shortcut below must not trust it.
  const leavingNarrowing =
    filtersActive() &&
    (route.chatId !== previous?.chatId || route.messageId !== null);
  if (leavingNarrowing) resetFilters();
  setPanelOpen(false);

  // Same chat, and the wanted message is already loaded: no request, just move to it.
  const sameChat = previous?.chatId === route.chatId && state.chat !== null;
  if (
    sameChat &&
    !leavingNarrowing &&
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

  const url =
    route.messageId === null
      ? windowUrl(route.chatId)
      : windowUrl(route.chatId, { around: route.messageId });
  try {
    // The window first: its errors carry a machine-readable `reason` (chat_not_found, message_not_found) the UI can translate,
    // which the plain-text 404 of GET /api/chats/{id} does not.
    const [windowRes, chatRes] = await Promise.all([
      apiFetch(url),
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
      windowUrl(chatId, { before: state.messages[0].id }),
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
      windowUrl(chatId, {
        after: state.messages[state.messages.length - 1].id,
      }),
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

/** Click on a chip: open or close that message's edit history / deletion note, fetching the detail the first time.
 *  Click on a sender's name: the rename dialog. */
async function onMessageClick(event) {
  const senderButton = event.target.closest?.(".cv-msg__sender--btn");
  if (senderButton) {
    openSenderDialog(Number(senderButton.dataset.senderId));
    return;
  }
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

/**
 * The header's block button is a toggle.
 *   not blocked -> "Block this chat": stop archiving from now on (PUT /api/ingest/blocklist/{id}).
 *   blocked     -> "Archive this chat again" (DELETE, idempotent): archiving resumes from the next message.
 * Already archived messages stay either way;
 * what arrived while the chat was blocked was never stored and cannot come back, which the unblock dialog says before the person commits.
 */
async function onToggleBlock() {
  const chat = state.chat;
  if (!chat) return;
  const blocked = state.blocked.has(chat.chat_id);
  const name = { name: chatName(chat) };
  const confirmed = blocked
    ? await showConfirm(t("cv.unblockMessage"), {
        title: fill(t("cv.unblockTitle"), name),
        confirmLabel: t("cv.unblockConfirm"),
      })
    : await showConfirm(t("cv.blockMessage"), {
        title: fill(t("cv.blockTitle"), name),
        confirmLabel: t("cv.blockConfirm"),
        danger: true,
      });
  if (!confirmed) return;

  try {
    const res = await apiFetch(`/api/ingest/blocklist/${chat.chat_id}`, {
      method: blocked ? "DELETE" : "PUT",
    });
    if (!res.ok) {
      await showAlert(describeError(await readErrorDetail(res)));
      return;
    }
    if (blocked) {
      // The rule may sit under another id than the one in the URL (a group that became a supergroup); the server's list is the truth.
      state.blocked.delete(chat.chat_id);
      await loadBlocklist();
    } else {
      // The rule may be stored under the chat's CURRENT id, so trust the response.
      const rule = await res.json();
      state.blocked.add(rule.chat_id ?? chat.chat_id);
      renderConversationHeader();
    }
    // Tell the Settings list (which only loads once) to re-read, so it never shows a stale "Chats not archived".
    document.dispatchEvent(new CustomEvent("televault:blocklistchanged"));
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

/**
 * Settings -> "Delete all stored data" announces itself with this event (js/views/settings.js): the archive is empty now,
 * but the conversation on screen still holds the old messages in memory, and the quiet refresh only ADDS what is new - it never re-checks older ones.
 * So drop everything that was loaded and leave the chat, exactly as after deleting one chat's data.
 * An event instead of an import keeps the two views independent (settings.js never needs to know the chat view exists).
 */
window.addEventListener("televault:archive-cleared", () => {
  state.detailCache.clear();
  state.openEdits.clear();
  state.openDeletion.clear();
  location.hash = chatRouteHash();
  openRoute({ chatId: null, messageId: null }); // the hashchange event does the same; this covers a hash that was already "#/chats"
  loadPanelPage({ reset: true });
});

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

  // A narrowed list is left alone: merging the unfiltered newest window into it would put non-matching messages in.
  if (
    state.convStatus === "ready" &&
    state.chat &&
    !state.hasNewer &&
    !filtersActive()
  ) {
    const seq = state.convSeq;
    try {
      const [windowRes, chatRes] = await Promise.all([
        apiFetch(`/api/chats/${state.chat.chat_id}/window`),
        apiFetch(`/api/chats/${state.chat.chat_id}`),
      ]);
      if (seq !== state.convSeq || !windowRes.ok) return;
      const data = await windowRes.json();
      // The reading position travels with the chat record.
      // When it moved (the owner read the chat elsewhere) the bar moves with it;
      // new messages need no special case - mergeNewest() redraws the list, and the bar with it.
      const previousPosition = state.chat.read_inbox_max_id;
      if (chatRes.ok) state.chat = await chatRes.json();
      mergeNewest(data.items);
      if (state.chat.read_inbox_max_id !== previousPosition) syncUnreadBar();
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

/**
 * How often the Chats tab quietly re-reads what the archiver stored (panel previews, counts, and the newest messages of the open chat).
 * 10 s is a compromise: fast enough that a new message feels "live", slow enough that a personal server never notices the requests (a few small GETs).
 */
const LIVE_REFRESH_MS = 10000;

/** True while nobody can see the Chats tab: the page is in a background tab / locked phone, or another tab of the app is open. */
function chatsTabHidden() {
  if (document.hidden) return true;
  return document.querySelector('.app-view[data-view="chats"]')?.hidden ?? true;
}

let liveRefreshRunning = false;

/**
 * One tick of the live refresh.
 * Skipped while the tab is not visible (no point, and a phone would be woken for nothing), and never overlaps itself, so a slow connection cannot pile requests up.
 *
 * It reuses refreshChatView() - the tab-switch refresh - on purpose:
 * that function already follows the rule that a refresh may only redraw when the answer DIFFERS and must keep scroll position,
 * so a timer calling it never rewrites a list while someone is reading it unless something really changed.
 * (The old "no timer" rule in app.js was about re-drawing blindly, which this does not do.)
 */
async function liveTick() {
  if (liveRefreshRunning || chatsTabHidden()) return;
  liveRefreshRunning = true;
  try {
    await refreshChatView();
  } finally {
    liveRefreshRunning = false;
  }
}

function startLiveRefresh() {
  setInterval(liveTick, LIVE_REFRESH_MS);
  // Coming back to the page (unlocking a phone, switching browser tabs) should not wait for the next tick.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) liveTick();
  });
}

/** Wire the panel's controls, draw the empty states, and open whatever the URL already names.
 *  Idempotent. */
let initialized = false;
function initChatView() {
  if (initialized) return;
  initialized = true;

  document
    .querySelectorAll(".chatview__panel-head .cv-switch__btn")
    .forEach((btn) => {
      btn.addEventListener("click", () => setMode(btn.dataset.mode));
    });
  initPanelResizer();
  document
    .getElementById("cv-scrim")
    ?.addEventListener("click", () => setPanelOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.panelOpen) {
      setPanelOpen(false);
      document.getElementById("cv-panel-toggle")?.focus();
    }
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
  loadOwner();
  loadDisplayNames();
  // Settings changed the list (or another tab did): re-read it so the header badge and button match.
  document.addEventListener("televault:blocklistchanged", () => {
    loadBlocklist();
  });

  window.addEventListener("hashchange", syncFromHash);
  syncFromHash();
  startLiveRefresh();

  // A link to the route that is ALREADY in the address bar changes nothing for the browser: no hashchange fires, so nothing would happen.
  // That bit "Show in context": open it, narrow the list again (All/Edited/Deleted) - the hash still names that message -
  // and the same link did nothing the second time.
  // Treat such a click as an explicit request to go there and open the route ourselves.
  // Modified clicks (new tab, new window) are left to the browser.
  document.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const link = /** @type {HTMLElement} */ (event.target).closest?.(
      'a[href^="#/chats"]',
    );
    if (!link || link.getAttribute("href") !== location.hash) return;
    const route = parseChatRoute(location.hash);
    if (!route) return;
    event.preventDefault();
    openRoute(route);
  });

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
