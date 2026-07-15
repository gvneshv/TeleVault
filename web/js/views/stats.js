/**
 * Stats view.
 *
 * Fetches GET /api/stats once and renders:
 *   - Global totals as a row of stat cards (messages, deleted, edited, chats, senders, archiving-since date)
 *   - A per-chat breakdown table, click-sortable by any column (defaults to message_count descending, matching the API's own default order)
 *
 * Percentages (deleted/edited as a % of total messages) are computed here,
 * not returned by the API — StatsOut's own docstring says this is deliberate, to avoid float precision noise in the API response.
 *
 * No filters, no pagination — this is a single-fetch dashboard, not a list view.
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";

const statsViewState = {
  initialized: false,
  lastData: null,
  // Client-side sort of per_chat - the API always returns it sorted by message_count descending (see StatsOut's docstring);
  // this tracks whatever the user last clicked, defaulting to that same order so the initial render matches what the API already gives us.
  sortKey: "message_count",
  sortDir: "desc",
};

/**
 * Format a percentage for display,
 * to 2 decimal places (whole-number rounding was misleading at real scale - e.g. 52/458931 rounded to "0%", hiding that there were any deleted messages at all).
 * Returns "0.00%" for a zero total rather than dividing by zero.
 * @param {number} part
 * @param {number} total
 * @returns {string}
 */
function formatPercent(part, total) {
  if (!total) return "0.00%";
  return `${((part / total) * 100).toFixed(2)}%`;
}

/** @param {string | null} iso @returns {string} */
function formatStatsDate(iso) {
  if (!iso) return "—";
  const locale = getCurrentLang() === "uk" ? "uk-UA" : "en-US";
  try {
    return new Date(iso).toLocaleDateString(locale, { dateStyle: "medium" });
  } catch {
    return iso;
  }
}

/**
 * Render the global totals as a row of stat cards.
 * @param {object} data - a StatsOut record from the API.
 * @returns {string}
 */
function renderStatCards(data) {
  const card = (label, value) => `
    <div class="stat-card">
      <span class="stat-card__value">${value}</span>
      <span class="stat-card__label">${label}</span>
    </div>
  `;

  return `
    <div class="stat-card-grid">
      ${card(t("stats.totalMessages"), data.total_messages)}
      ${card(t("stats.totalDeleted"), `${data.total_deleted} (${formatPercent(data.total_deleted, data.total_messages)})`)}
      ${card(t("stats.totalEdited"), `${data.total_edited} (${formatPercent(data.total_edited, data.total_messages)})`)}
      ${card(t("stats.totalChats"), data.total_chats)}
      ${card(t("stats.totalSenders"), data.total_senders)}
      ${card(t("stats.archivingSince"), formatStatsDate(data.archiving_since))}
    </div>
  `;
}

/**
 * Sort a copy of per_chat by the given key/direction.
 * Never mutates the original array - each click re-sorts fresh from the state's own copy, so there's no cumulative drift from repeated re-sorts.
 *
 * @param {object[]} perChat
 * @param {string} key - one of "name", "message_count", "deleted_count", "edited_count", "last_message_at"
 * @param {"asc" | "desc"} dir
 * @returns {object[]}
 */
function sortPerChat(perChat, key, dir) {
  const sorted = [...perChat];
  const mul = dir === "asc" ? 1 : -1;

  sorted.sort((a, b) => {
    if (key === "name") {
      const nameA = a.name ?? String(a.chat_id);
      const nameB = b.name ?? String(b.chat_id);
      return mul * nameA.localeCompare(nameB);
    }
    if (key === "last_message_at") {
      const dateA = a.last_message_at
        ? new Date(a.last_message_at).getTime()
        : 0;
      const dateB = b.last_message_at
        ? new Date(b.last_message_at).getTime()
        : 0;
      return mul * (dateA - dateB);
    }
    // message_count, deleted_count, edited_count - plain numeric columns.
    return mul * ((a[key] ?? 0) - (b[key] ?? 0));
  });

  return sorted;
}

/**
 * Render the per-chat breakdown as a sortable table.
 * Column headers are clickable (data-sort-key) - see wireSortableHeaders() below for the click handling, kept separate since this function only builds markup.
 *
 * @param {object[]} perChat - list of ChatStatRow records.
 * @returns {string}
 */
function renderPerChatTable(perChat) {
  if (perChat.length === 0) {
    return `<div class="empty-state">${t("stats.empty")}</div>`;
  }

  const sorted = sortPerChat(
    perChat,
    statsViewState.sortKey,
    statsViewState.sortDir,
  );

  const rows = sorted
    .map((row) => {
      const typeLabel = row.chat_type ? t(`common.type.${row.chat_type}`) : "";
      return `
        <tr>
          <td>
            ${escapeHtml(row.name ?? String(row.chat_id))}
            ${typeLabel ? `<span class="info-badge">${typeLabel}</span>` : ""}
          </td>
          <td class="stats-table__num">${row.message_count}</td>
          <td class="stats-table__num">${row.deleted_count}</td>
          <td class="stats-table__num">${row.edited_count}</td>
          <td class="stats-table__date">${formatStatsDate(row.last_message_at)}</td>
        </tr>
      `;
    })
    .join("");

  // Each header shows a ▲/▼ only when it's the active sort column - a plain, low-key affordance rather than icons on every column,
  // which would be noise until you actually click one.
  const sortArrow = (key) =>
    statsViewState.sortKey === key
      ? statsViewState.sortDir === "asc"
        ? " ▲"
        : " ▼"
      : "";

  const th = (key, label, extraClass = "") => `
    <th class="stats-table__sortable ${extraClass}" data-sort-key="${key}">${label}${sortArrow(key)}</th>
  `;

  return `
    <div class="stats-table-wrapper">
      <table class="stats-table">
        <thead>
          <tr>
            ${th("name", t("stats.tableChat"))}
            ${th("message_count", t("stats.tableMessages"), "stats-table__num")}
            ${th("deleted_count", t("stats.tableDeleted"), "stats-table__num")}
            ${th("edited_count", t("stats.tableEdited"), "stats-table__num")}
            ${th("last_message_at", t("stats.tableLastSeen"), "stats-table__date")}
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

/**
 * Wire click handlers onto the sortable column headers just rendered.
 * Clicking the already-active column flips its direction; clicking a different column switches to it with a sensible default direction
 * (numeric/date columns start descending - "most/latest first" is usually what you want; the chat name column starts ascending - alphabetical).
 *
 * @param {HTMLElement} root
 * @param {object} data - the full StatsOut record, so re-rendering after a
 *   sort change doesn't need a re-fetch.
 */
function wireSortableHeaders(root, data) {
  root.querySelectorAll(".stats-table__sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (statsViewState.sortKey === key) {
        statsViewState.sortDir =
          statsViewState.sortDir === "asc" ? "desc" : "asc";
      } else {
        statsViewState.sortKey = key;
        statsViewState.sortDir = key === "name" ? "asc" : "desc";
      }
      renderStatsView(root, data);
    });
  });
}

/**
 * Render the view's current state from already-fetched data — used both after loading and after a language change
 * (no re-fetch needed for a language switch; the underlying stats haven't changed).
 *
 * @param {HTMLElement} root
 * @param {object} data - a StatsOut record from the API.
 */
function renderStatsView(root, data) {
  root.innerHTML = `
    ${renderStatCards(data)}
    <h2 class="stats-section-title">${t("stats.perChatTitle")}</h2>
    ${renderPerChatTable(data.per_chat)}
  `;
  wireSortableHeaders(root, data);
}

/** Fetch stats once, cache, and render. */
async function loadStats(root) {
  root.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;

  let data;
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch {
    root.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
    return;
  }

  statsViewState.lastData = data;
  renderStatsView(root, data);
}

/** Entry point called by app.js the first time the Stats tab is opened. */
function initStatsView() {
  if (statsViewState.initialized) return;
  statsViewState.initialized = true;

  const root = document.getElementById("stats-root");
  if (root) loadStats(root);
}

export { initStatsView };

document.addEventListener("televault:langchange", () => {
  if (!statsViewState.initialized) return;
  const root = document.getElementById("stats-root");
  if (root && statsViewState.lastData) {
    renderStatsView(root, statsViewState.lastData);
  }
});
