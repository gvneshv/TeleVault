/**
 * Settings view.
 *
 * Four cards:
 *   1. A link out to the dedicated telegram-setup.html page (see that file's own module docstring for why the Telegram/archive wizard lives there instead of here).
 *   2. "Chats not archived": the ingestion blocklist (api/routes/ingest.py, policy in handlers/ingest_filter.py) -
 *      pick an already-archived chat to stop archiving it, or take one off the list.
 *      It is a cheap database read, so unlike the devices card it loads as soon as the tab opens.
 *      Only chats the archive already knows are offered: a new chat is always archived until you block it here.
 *   3. "Ignore rules": message ignore rules (api/routes/ingest.py, policy in handlers/ingest_filter.py) - a phrase, and either every chat or the chats ticked in the form.
 *      A message whose TEXT contains the phrase (ignoring case; plain substring, never a regex; captions are not matched) is no longer archived.
 *      Only future messages are affected.
 *      Loads with the tab like the blocklist card.
 *      The form's own state (what was typed, ticked, filtered) lives in settingsViewState so a re-render does not reset it;
 *      typing, ticking and filtering update the page directly instead of re-rendering, so focus and the scroll position of the chat list are never lost mid-edit.
 *      The size limits come from the server's response (max_length, max_chats, max_count) - nothing here repeats utils/validation.py's numbers.
 *   3b. "Automatic cleanup" (retention policy, api/routes/archive.py, applied by retention.py inside the archiver): delete archived messages older than N months.
 *      Irreversible, so the card never lets a number be saved blind: "Preview" asks the server how many messages WOULD go (nothing is deleted),
 *      only then can the policy be switched on, and the previewed count is sent back as the confirmation (the server counts again and refuses a stale one).
 *      Changing the months or the checkbox throws the preview away.
 *      Off by default; messages deleted in Telegram are kept unless the owner unticks the box.
 *   3c. "Files" (file-type policy, api/routes/media.py): which of photo / video / voice / audio / document are stored at all.
 *      One account-wide choice for the live archiver and every backfill; everything on by default.
 *      A type that is off is still recorded, just not downloaded.
 *      Plain checkboxes plus one Save button (a click on a box changes nothing by itself); the server answers with how many waiting files the change re-sorted.
 *   4. "Telegram devices": the account's Telegram sessions (the same list as Telegram's own Settings -> Devices)
 *      with buttons to end one device or every device except TeleVault's own.
 *      Every link creates a new Telegram session and nothing used to end the old ones, so they piled up.
 *
 * The devices card is deliberately NOT loaded when the tab opens: loading it connects to Telegram as the account,
 * which needs the archiver and any backfill to be stopped and shows IP addresses - a person should ask for that,
 * not have it happen (or fail with a 409) every time they glance at Settings.
 * So it starts as a single "Show my devices" button, and its list lives in memory only (never localStorage: it contains locations).
 *
 * This tab is where future account-settings features land as they're built - change password, and whatever else comes up - each as its own card.
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 */

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { formatBytes } from "../lib/media-view.js";
import { apiFetch } from "../lib/auth.js";
import { describeError, describeSessionError } from "../lib/errors.js";
import { archiveFixLink } from "../lib/archive-error.js";
import { showAlert, showConfirm, showTypedConfirm } from "../lib/dialog.js";
import { chatDisplayName, fetchDisplayNames } from "../lib/display-names.js";

const settingsViewState = {
  initialized: false,
  blocklist: {
    // null = not loaded (yet, or the load failed - see `error`), otherwise the rules from GET /api/ingest/blocklist.
    items: null,
    // Every archived chat as {chat_id, name} (GET /api/chats/options) - what the picker offers, minus what is already blocked.
    chats: [],
    // sender id -> the owner's own label (lib/display-names.js); a private chat is shown under its person's label, then Telegram's name in brackets.
    labels: new Map(),
    maxCount: null,
    loading: false,
    // The chat id (string) being blocked/unblocked, or null.
    busy: null,
    // What the picker currently shows; kept here so a re-render (language change, a finished request) does not reset it.
    selected: "",
    // Whether the list of blocked chats is open.
    // Closed by default so a long list does not stretch the page (same as the ignore rules);
    // kept while the app stays open, not across a reload.
    showList: false,
    // "blocked" | "unblocked" | null: a one-line confirmation of the last change, because the list that shows it is usually closed.
    notice: null,
    error: null,
    // {href, label} when the error is one the person can fix themselves (no archive database yet), else null.
    fix: null,
  },
  ignore: {
    // null = not loaded (yet, or the load failed - see `error`), otherwise the rules from GET /api/ingest/ignore-rules.
    items: null,
    // Every archived chat as {chat_id, name} (GET /api/chats/options): what the "only these chats" list offers.
    chats: [],
    // Same as blocklist.labels: the owner's own names for private chats.
    labels: new Map(),
    // {maxCount, maxLength, maxChats} from the same response; null until loaded.
    limits: null,
    loading: false,
    // "add" while a rule is being created, the rule id (string) while one is being removed, or null.
    busy: null,
    // The form, kept here so a re-render (language change, a finished request) does not reset what was typed or ticked.
    form: { pattern: "", scope: "all", chatIds: new Set(), filter: "" },
    // The list of existing rules is collapsed until asked for ("Show all rules"):
    // with a few dozen rules it pushed the rest of Settings off the screen and grew with every rule added.
    // Not remembered across page loads on purpose - collapsed is the default every time.
    showList: false,
    // "added" | "removed" | null: a one-line confirmation of the last change.
    // With the list collapsed, adding a rule would otherwise change nothing the person can see but a number on a button.
    // Cleared by the next change, by typing in the phrase field, and by opening or closing the list.
    notice: null,
    error: null,
    // {href, label} when the error is one the person can fix themselves (no archive database yet), else null.
    fix: null,
  },
  retention: {
    // null = not loaded (yet, or the load failed - see `error`), otherwise the answer of GET /api/archive/retention.
    policy: null,
    loading: false,
    // "preview" | "save" | "off" while that request runs, else null.
    busy: null,
    // The form, kept here so a re-render does not reset it.
    // Seeded once from the saved policy.
    form: { months: "6", keepDeleted: true },
    seeded: false,
    // The last preview {months, keepDeleted, count, total, oldest, newest}, or null.
    // Valid only for the form values it was made with.
    preview: null,
    // "saved" | "off" | null: a one-line confirmation of the last change.
    notice: null,
    error: null,
    // {href, label} when the error is one the person can fix themselves (no archive database yet / not migrated), else null.
    fix: null,
  },
  files: {
    // null = not loaded (yet, or the load failed - see `error`), otherwise the answer of GET /api/media/policy ({photo, video, voice, audio, document}).
    policy: null,
    loading: false,
    busy: false,
    // The ticks, kept here so a re-render does not reset them.
    // Seeded from the saved policy on every load.
    form: {
      photo: true,
      video: true,
      voice: true,
      audio: true,
      document: true,
    },
    // {skipped, queued} of the last save, or null: a one-line confirmation.
    notice: null,
    error: null,
    // {href, label} when the error is one the person can fix themselves (not migrated yet), else null.
    fix: null,
    // The answer of GET /api/media/usage (stored_bytes, limit_bytes, counts, ...), or null when not loaded / failed.
    // Never blocks the ticks.
    usage: null,
    // True while the "Try again" request runs, and its one-line result ({requeued}) afterwards.
    retrying: false,
    retried: null,
  },
  data: {
    // True while DELETE /api/archive/data runs (it can take a while on a big archive); the button is disabled meanwhile.
    busy: false,
    error: null,
  },
  devices: {
    // null = never loaded (only the "Show my devices" button), otherwise the last list from GET /api/telegram/sessions.
    list: null,
    loading: false,
    // Id of the device being ended, "others" while ending all other devices, or null.
    busy: null,
    error: null,
  },
};

/** A date-time in the UI language's own style (device sessions and blocklist entries share it). @param {string | null} iso @returns {string} */
function formatDate(iso) {
  if (!iso) return "-";
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

/** "Pixel 8" for the title, "Telegram Android 10.1" for the subtitle - either may be empty for odd sessions. */
function deviceTitle(d) {
  return d.device_model || d.platform || t("settings.deviceUnnamed");
}

function renderDeviceRow(d) {
  const busy = settingsViewState.devices.busy !== null;
  const app = [d.app_name, d.app_version].filter(Boolean).join(" ");
  const system = [d.platform, d.system_version].filter(Boolean).join(" ");
  const place = [d.country, d.region].filter(Boolean).join(", ");
  const facts = [
    app,
    system,
    place,
    d.ip,
    `${t("settings.deviceSignedIn")}: ${formatDate(d.created_at)}`,
    `${t("settings.deviceLastActive")}: ${formatDate(d.active_at)}`,
  ].filter(Boolean);

  // TeleVault's own session gets a badge and NO button: ending it is "Unlink Telegram" (setup page), and the server refuses it too.
  const action = d.current
    ? `<span class="patina-badge">${t("settings.deviceThisSession")}</span>`
    : `<button type="button" class="modal__btn modal__btn--danger" data-device-id="${escapeHtml(d.id)}" ${busy ? "disabled" : ""}>
         ${settingsViewState.devices.busy === d.id ? t("settings.deviceEnding") : t("settings.deviceEnd")}
       </button>`;

  return `
    <li class="device-row">
      <div class="device-row__body">
        <div class="device-row__name">${escapeHtml(deviceTitle(d))}</div>
        <div class="device-row__meta">${facts.map(escapeHtml).join(" · ")}</div>
      </div>
      <div class="device-row__action">${action}</div>
    </li>
  `;
}

/** "Your stored data": the one irreversible button of the page.
 *  What it removes and keeps is spelled out BEFORE the person clicks. */
function renderDataCard() {
  const s = settingsViewState.data;
  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}</p>`
    : "";
  return `
    <section class="settings-card settings-card--danger" id="data-card">
      <h2>${t("settings.dataTitle")}</h2>
      <p class="settings-help">${t("settings.dataIntro")}</p>
      <div class="settings-actions">
        <button type="button" id="data-clear" class="modal__btn modal__btn--danger" ${s.busy ? "disabled" : ""}>${s.busy ? t("settings.dataClearing") : t("settings.dataClear")}</button>
      </div>
      ${error}
    </section>
  `;
}

function renderDevicesCard() {
  const s = settingsViewState.devices;
  const busy = s.loading || s.busy !== null;

  let body = "";
  if (s.list !== null) {
    const others = s.list.filter((d) => !d.current);
    body = s.list.length
      ? `<ul class="device-list">${s.list.map(renderDeviceRow).join("")}</ul>`
      : `<p class="settings-help">${t("settings.devicesEmpty")}</p>`;
    if (others.length > 0) {
      body += `
        <button type="button" id="devices-end-others" class="modal__btn modal__btn--danger" ${busy ? "disabled" : ""}>
          ${s.busy === "others" ? t("settings.deviceEnding") : t("settings.devicesEndOthers")}
        </button>`;
    }
  }

  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}</p>`
    : "";
  const label = s.loading
    ? t("common.loading")
    : s.list === null
      ? t("settings.devicesLoad")
      : t("settings.devicesRefresh");

  return `
    <section class="settings-card settings-card--devices" id="devices-card">
      <h2>${t("settings.devicesTitle")}</h2>
      <p class="settings-help">${t("settings.devicesIntro")}</p>
      <div class="settings-actions">
        <button type="button" id="devices-load" class="${s.list === null ? "modal__btn modal__btn--primary" : "settings-refresh-btn"}" ${busy ? "disabled" : ""}>${label}</button>
      </div>
      ${error}
      ${body}
    </section>
  `;
}

/**
 * A chat as the owner knows it: "label (Telegram name)" for a private chat that has a label, otherwise its name, otherwise `fallback`.
 * `labels` is the card's own copy of the owner's labels (each card loads its own); the blocklist's by default.
 */
function labelled(chat, fallback, labels = settingsViewState.blocklist.labels) {
  const name = chat.name || "";
  const label = chatDisplayName(labels, chat, "");
  if (!label) return name || fallback;
  return name ? `${label} (${name})` : label;
}

/** One blocked chat: its name (as it was when blocked), id and date, and the button that lifts the block. */
function renderBlockedRow(rule) {
  const s = settingsViewState.blocklist;
  const id = String(rule.chat_id);
  const busy = s.busy !== null || s.loading;
  const facts = [
    `ID ${id}`,
    `${t("settings.blocklistBlockedOn")}: ${formatDate(rule.blocked_at)}`,
  ];
  return `
    <li class="device-row">
      <div class="device-row__body">
        <div class="device-row__name">${escapeHtml(labelled(rule, t("settings.blocklistUnnamed")))}</div>
        <div class="device-row__meta">${facts.map(escapeHtml).join(" · ")}</div>
      </div>
      <div class="device-row__action">
        <button type="button" class="modal__btn" data-unblock-id="${escapeHtml(id)}" ${busy ? "disabled" : ""}>
          ${s.busy === id ? t("settings.blocklistRemoving") : t("settings.blocklistRemove")}
        </button>
      </div>
    </li>
  `;
}

/** One line confirming the last block or unblock (always in the markup, hidden when empty, so it can be cleared without a re-render). */
function renderBlocklistNotice() {
  const key = {
    blocked: "settings.blocklistBlocked",
    unblocked: "settings.blocklistUnblocked",
  }[settingsViewState.blocklist.notice ?? ""];
  return `<p class="settings-help ignore-notice" id="blocklist-notice" role="status" ${key ? "" : "hidden"}>${key ? t(key) : ""}</p>`;
}

/** The label of the show/hide button: the count belongs on the closed state, where the chats themselves are out of sight. */
function blocklistToggleLabel() {
  const s = settingsViewState.blocklist;
  return s.showList
    ? t("settings.blocklistHide")
    : t("settings.blocklistShowAll").replace(
        "{n}",
        String(s.items?.length ?? 0),
      );
}

/**
 * The blocked chats behind a "Show all blocked chats (N)" button - the same pattern as the ignore rules.
 * The list is always in the markup and only hidden, so aria-controls points at a real element and opening it needs no re-render (the button keeps focus).
 */
function renderBlockedList() {
  const s = settingsViewState.blocklist;
  return `
    <button type="button" class="ignore-toggle" id="blocklist-toggle" aria-expanded="${s.showList}" aria-controls="blocklist-list">${escapeHtml(blocklistToggleLabel())}</button>
    <ul class="device-list" id="blocklist-list" ${s.showList ? "" : "hidden"}>${(s.items ?? []).map(renderBlockedRow).join("")}</ul>
  `;
}

function renderBlocklistCard() {
  const s = settingsViewState.blocklist;
  const busy = s.busy !== null || s.loading;

  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}${
        s.fix
          ? ` <a href="${s.fix.href}" class="settings-fix-link">${escapeHtml(s.fix.label)}</a>`
          : ""
      }</p>`
    : "";

  let body =
    s.loading && s.items === null
      ? `<p class="settings-help">${t("common.loading")}</p>`
      : "";
  if (s.items !== null) {
    const blockedIds = new Set(s.items.map((r) => String(r.chat_id)));
    const choices = s.chats.filter((c) => !blockedIds.has(String(c.chat_id)));
    // A stale selection (the chat was just blocked, or vanished) must not stay "selected" with no matching <option>.
    const selected = choices.some((c) => String(c.chat_id) === s.selected)
      ? s.selected
      : "";
    const options = choices
      .map(
        (c) =>
          `<option value="${escapeHtml(String(c.chat_id))}" ${String(c.chat_id) === selected ? "selected" : ""}>${escapeHtml(labelled(c, `ID ${c.chat_id}`))}</option>`,
      )
      .join("");

    body = choices.length
      ? `<div class="blocklist-add">
           <select id="blocklist-select" class="blocklist-select" aria-label="${escapeHtml(t("settings.blocklistChatLabel"))}" ${busy ? "disabled" : ""}>
             <option value="">${t("settings.blocklistPick")}</option>${options}
           </select>
           <button type="button" id="blocklist-add" class="modal__btn modal__btn--primary" ${busy || !selected ? "disabled" : ""}>
             ${s.busy === "add" ? t("settings.blocklistAdding") : t("settings.blocklistAdd")}
           </button>
         </div>`
      : `<p class="settings-help">${t("settings.blocklistNoChats")}</p>`;

    body += renderBlocklistNotice();
    body += s.items.length
      ? renderBlockedList()
      : `<p class="settings-help">${t("settings.blocklistEmpty")}</p>`;
  }

  return `
    <section class="settings-card settings-card--blocklist" id="blocklist-card">
      <h2>${t("settings.blocklistTitle")}</h2>
      <p class="settings-help">${t("settings.blocklistIntro")}</p>
      ${error}
      ${body}
    </section>
  `;
}

function renderRoot(root) {
  root.innerHTML = `
    <section class="settings-card">
      <h2>${t("settings.telegramCardTitle")}</h2>
      <p class="settings-help">${t("settings.telegramCardBody")}</p>
      <a href="/telegram-setup.html" class="modal__btn modal__btn--primary">${t("settings.telegramCardLink")}</a>
    </section>
    ${renderBlocklistCard()}
    ${renderIgnoreCard()}
    ${renderRetentionCard()}
    ${renderFilesCard()}
    ${renderDataCard()}
    ${renderDevicesCard()}
  `;
  attachBlocklistHandlers(root);
  attachIgnoreHandlers(root);
  attachRetentionHandlers(root);
  attachFilesHandlers(root);
  attachDataHandlers(root);
  attachDevicesHandlers(root);
}

/** Put an error response into the blocklist card: translated, and with the "create your archive" link when that is what is missing. */
async function failBlocklist(res) {
  const s = settingsViewState.blocklist;
  const body = await res.json().catch(() => ({}));
  s.error = describeError(body.detail);
  s.fix = archiveFixLink(body.detail);
}

/**
 * (Re)load the blocklist and the chats the picker offers.
 * Never throws; problems land in the card.
 *
 * @param {HTMLElement} root
 */
async function loadBlocklist(root) {
  const s = settingsViewState.blocklist;
  s.error = null;
  s.fix = null;
  s.loading = true;
  renderRoot(root);

  try {
    const [rules, chats, labels] = await Promise.all([
      apiFetch("/api/ingest/blocklist"),
      apiFetch("/api/chats/options"),
      fetchDisplayNames(),
    ]);
    if (!rules.ok) {
      s.items = null;
      await failBlocklist(rules);
    } else if (!chats.ok) {
      s.items = null;
      await failBlocklist(chats);
    } else {
      const data = await rules.json();
      s.items = data.items;
      s.maxCount = data.max_count;
      s.chats = await chats.json();
      s.labels = labels;
    }
  } catch {
    s.items = null;
    s.error = t("common.error");
  }
  s.loading = false;
  renderRoot(root);
}

/**
 * Block (PUT) or unblock (DELETE) one chat, then reload both lists so the screen shows what the server now holds.
 *
 * @param {HTMLElement} root
 * @param {string} chatId
 * @param {boolean} block
 */
async function changeBlock(root, chatId, block) {
  const s = settingsViewState.blocklist;
  s.error = null;
  s.fix = null;
  s.notice = null;
  s.busy = block ? "add" : chatId;
  renderRoot(root);

  let ok = false;
  try {
    // The id came from the server as a number; encodeURIComponent is belt and braces for the URL.
    const res = await apiFetch(
      `/api/ingest/blocklist/${encodeURIComponent(chatId)}`,
      { method: block ? "PUT" : "DELETE" },
    );
    if (res.ok) ok = true;
    else await failBlocklist(res);
  } catch {
    s.error = t("common.error");
  }
  s.busy = null;
  if (!ok) {
    renderRoot(root);
    return;
  }
  s.selected = "";
  s.notice = block ? "blocked" : "unblocked";
  await loadBlocklist(root);
  // The chat view keeps its own copy of the list (its header badge and block button) - tell it to re-read.
  document.dispatchEvent(new CustomEvent("televault:blocklistchanged"));
}

function attachBlocklistHandlers(root) {
  const s = settingsViewState.blocklist;

  document
    .getElementById("blocklist-select")
    ?.addEventListener("change", (e) => {
      s.selected = e.target.value;
      s.notice = null;
      renderRoot(root);
    });

  // Show or hide the list without a re-render, so the button keeps focus.
  document
    .getElementById("blocklist-toggle")
    ?.addEventListener("click", (e) => {
      s.showList = !s.showList;
      s.notice = null;
      const notice = document.getElementById("blocklist-notice");
      if (notice) {
        notice.hidden = true;
        notice.textContent = "";
      }
      const list = document.getElementById("blocklist-list");
      if (list) list.hidden = !s.showList;
      const button = /** @type {HTMLElement} */ (e.currentTarget);
      button.setAttribute("aria-expanded", String(s.showList));
      button.textContent = blocklistToggleLabel();
    });

  document.getElementById("blocklist-add")?.addEventListener("click", () => {
    if (s.selected) changeBlock(root, s.selected, true);
  });

  document.querySelectorAll("[data-unblock-id]").forEach((btn) => {
    btn.addEventListener("click", () =>
      changeBlock(root, btn.dataset.unblockId, false),
    );
  });
}

// ---------------------------------------------------------------------------
// Ignore rules card
// ---------------------------------------------------------------------------

// A rule limited to many chats would make one row a wall of names; show this many and count the rest.
const IGNORE_CHATS_SHOWN = 5;

/** Where a rule applies, as one line: "All chats", or "Chats: A, B, C and 4 more". */
function ignoreScopeText(rule) {
  if (rule.all_chats) return t("settings.ignoreScopeAll");
  const labels = settingsViewState.ignore.labels;
  const names = rule.chats.map((c) => labelled(c, `ID ${c.chat_id}`, labels));
  const shown = names.slice(0, IGNORE_CHATS_SHOWN).join(", ");
  const rest = names.length - IGNORE_CHATS_SHOWN;
  return `${t("settings.ignoreScopeChats")}: ${shown}${
    rest > 0 ? ` ${t("settings.ignoreMore").replace("{n}", String(rest))}` : ""
  }`;
}

/** One rule: its phrase as typed, where it applies and when it was added, and the button that removes it. */
function renderIgnoreRow(rule) {
  const s = settingsViewState.ignore;
  const id = String(rule.id);
  const busy = s.busy !== null || s.loading;
  const facts = [
    ignoreScopeText(rule),
    `${t("settings.ignoreAddedOn")}: ${formatDate(rule.created_at)}`,
  ];
  return `
    <li class="device-row">
      <div class="device-row__body">
        <div class="device-row__name ignore-rule__pattern">${escapeHtml(rule.pattern)}</div>
        <div class="device-row__meta">${facts.map(escapeHtml).join(" · ")}</div>
      </div>
      <div class="device-row__action">
        <button type="button" class="modal__btn" data-ignore-remove-id="${escapeHtml(id)}" ${busy ? "disabled" : ""}>
          ${s.busy === id ? t("settings.ignoreRemoving") : t("settings.ignoreRemove")}
        </button>
      </div>
    </li>
  `;
}

/** True when the form holds something the server would accept (it still checks everything itself). */
function ignoreFormComplete() {
  const f = settingsViewState.ignore.form;
  return (
    f.pattern.trim().length > 0 && (f.scope === "all" || f.chatIds.size > 0)
  );
}

function renderIgnoreForm() {
  const s = settingsViewState.ignore;
  const f = s.form;
  const busy = s.busy !== null || s.loading;
  const limits = s.limits;
  const filter = f.filter.trim().toLocaleLowerCase();

  const boxes = s.chats
    .map((c) => {
      const id = String(c.chat_id);
      const name = labelled(c, `ID ${id}`, s.labels);
      const shown = !filter || name.toLocaleLowerCase().includes(filter);
      return `<label class="ignore-chat" data-ignore-chat-name="${escapeHtml(name.toLocaleLowerCase())}" ${shown ? "" : "hidden"}>
          <input type="checkbox" value="${escapeHtml(id)}" data-ignore-chat ${f.chatIds.has(id) ? "checked" : ""} ${busy ? "disabled" : ""}>
          <span>${escapeHtml(name)}</span>
        </label>`;
    })
    .join("");

  // With no archived chat to pick there is nothing to limit a rule to: "all chats" is the only choice.
  const canPick = s.chats.length > 0;
  const scope = canPick ? f.scope : "all";

  return `
    <form class="ignore-form" id="ignore-form" novalidate>
      <label class="settings-field ignore-field">
        <span>${t("settings.ignorePatternLabel")}</span>
        <input type="text" id="ignore-pattern" maxlength="${limits.maxLength}" autocomplete="off" spellcheck="false"
               placeholder="${escapeHtml(t("settings.ignorePatternPlaceholder"))}" value="${escapeHtml(f.pattern)}" ${busy ? "disabled" : ""}>
      </label>
      <fieldset class="ignore-scope">
        <legend>${t("settings.ignoreScopeLegend")}</legend>
        <label><input type="radio" name="ignore-scope" value="all" ${scope === "all" ? "checked" : ""} ${busy ? "disabled" : ""}> ${t("settings.ignoreScopeAllChats")}</label>
        <label><input type="radio" name="ignore-scope" value="chats" ${scope === "chats" ? "checked" : ""} ${busy || !canPick ? "disabled" : ""}> ${t("settings.ignoreScopeSelected")}</label>
      </fieldset>
      ${canPick ? "" : `<p class="settings-help ignore-nochats">${t("settings.ignoreNoChats")}</p>`}
      <div class="ignore-chats" id="ignore-chats" ${scope === "chats" ? "" : "hidden"}>
        <input type="search" id="ignore-chat-filter" class="ignore-chat-filter" autocomplete="off"
               placeholder="${escapeHtml(t("settings.ignoreFilterPlaceholder"))}" aria-label="${escapeHtml(t("settings.ignoreFilterPlaceholder"))}" value="${escapeHtml(f.filter)}">
        <div class="ignore-chat-list">${boxes}</div>
      </div>
      <button type="submit" id="ignore-add" class="modal__btn modal__btn--primary" ${busy || !ignoreFormComplete() ? "disabled" : ""}>
        ${s.busy === "add" ? t("settings.ignoreAdding") : t("settings.ignoreAdd")}
      </button>
    </form>
  `;
}

function renderIgnoreCard() {
  const s = settingsViewState.ignore;

  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}${
        s.fix
          ? ` <a href="${s.fix.href}" class="settings-fix-link">${escapeHtml(s.fix.label)}</a>`
          : ""
      }</p>`
    : "";

  let body =
    s.loading && s.items === null
      ? `<p class="settings-help">${t("common.loading")}</p>`
      : "";
  if (s.items !== null && s.limits) {
    body =
      renderIgnoreForm() +
      renderIgnoreNotice() +
      (s.items.length
        ? renderIgnoreList()
        : `<p class="settings-help">${t("settings.ignoreEmpty")}</p>`);
  }

  return `
    <section class="settings-card settings-card--ignore" id="ignore-card">
      <h2>${t("settings.ignoreTitle")}</h2>
      <p class="settings-help">${t("settings.ignoreIntro")}</p>
      ${renderIgnoreEditTip()}
      ${error}
      ${body}
    </section>
  `;
}

/** One line confirming the last add or remove.
 *  Always in the markup (hidden when empty) so attachIgnoreHandlers can clear it without a re-render. */
function renderIgnoreNotice() {
  const key = {
    added: "settings.ignoreAdded",
    removed: "settings.ignoreRemoved",
  }[settingsViewState.ignore.notice ?? ""];
  return `<p class="settings-help ignore-notice" id="ignore-notice" role="status" ${key ? "" : "hidden"}>${key ? t(key) : ""}</p>`;
}

/** The label of the show/hide button: the count belongs on the collapsed state, where the rules themselves are out of sight. */
function ignoreToggleLabel() {
  const s = settingsViewState.ignore;
  return s.showList
    ? t("settings.ignoreHide")
    : t("settings.ignoreShowAll").replace("{n}", String(s.items?.length ?? 0));
}

/**
 * The existing rules behind a "Show all rules (N)" button.
 * The list is always in the markup and only hidden, so aria-controls points at a real element and opening it does not need a re-render
 * (which would drop focus from the button).
 */
function renderIgnoreList() {
  const s = settingsViewState.ignore;
  return `
    <button type="button" class="ignore-toggle" id="ignore-toggle" aria-expanded="${s.showList}" aria-controls="ignore-list">${escapeHtml(ignoreToggleLabel())}</button>
    <ul class="device-list" id="ignore-list" ${s.showList ? "" : "hidden"}>${(s.items ?? []).map(renderIgnoreRow).join("")}</ul>
  `;
}

/**
 * The small "? How rules affect edits" hint under the card's intro.
 *
 * What an edit does is the least obvious part of the feature and too long for the intro, so it sits behind a button instead.
 * The bubble is shown by CSS on hover and on keyboard focus; a click (the only way on a touch screen) pins it open, see attachIgnoreEditTip().
 * It is a plain button + aria-describedby, not a native title="", because a title cannot hold two paragraphs, is not reachable by touch or keyboard, and cannot be styled.
 */
function renderIgnoreEditTip() {
  return `
    <div class="info-tip" id="ignore-edit-tip">
      <button type="button" class="info-tip__btn" id="ignore-edit-tip-btn"
        aria-expanded="false" aria-describedby="ignore-edit-tip-bubble"
        aria-label="${escapeHtml(t("settings.ignoreEditsLabel"))}">?</button>
      <span class="info-tip__label">${t("settings.ignoreEditsLabel")}</span>
      <div class="info-tip__bubble" id="ignore-edit-tip-bubble" role="tooltip">
        <p>${t("settings.ignoreEditsDropped")}</p>
        <p>${t("settings.ignoreEditsRevived")}</p>
      </div>
    </div>
  `;
}

/** Put an error response into the ignore-rules card: translated, and with the "create your archive" link when that is what is missing. */
async function failIgnore(res) {
  const s = settingsViewState.ignore;
  const body = await res.json().catch(() => ({}));
  s.error = describeError(body.detail);
  s.fix = archiveFixLink(body.detail);
}

/**
 * (Re)load the rules and the chats the form offers.
 * Never throws; problems land in the card.
 * What was typed and ticked is kept, except for chats that no longer exist.
 *
 * @param {HTMLElement} root
 */
async function loadIgnoreRules(root) {
  const s = settingsViewState.ignore;
  s.error = null;
  s.fix = null;
  s.loading = true;
  renderRoot(root);

  try {
    const [rules, chats, labels] = await Promise.all([
      apiFetch("/api/ingest/ignore-rules"),
      apiFetch("/api/chats/options"),
      fetchDisplayNames(),
    ]);
    if (!rules.ok) {
      s.items = null;
      await failIgnore(rules);
    } else if (!chats.ok) {
      s.items = null;
      await failIgnore(chats);
    } else {
      const data = await rules.json();
      s.items = data.items;
      s.limits = {
        maxCount: data.max_count,
        maxLength: data.max_length,
        maxChats: data.max_chats,
      };
      s.chats = await chats.json();
      s.labels = labels;
      const known = new Set(s.chats.map((c) => String(c.chat_id)));
      s.form.chatIds = new Set(
        [...s.form.chatIds].filter((id) => known.has(id)),
      );
    }
  } catch {
    s.items = null;
    s.error = t("common.error");
  }
  s.loading = false;
  renderRoot(root);
}

let ignoreEditTipBound = false;

/** Open or close the edit hint and keep aria-expanded and the CSS state in step.
 *  Opening clears an earlier Escape. */
function setIgnoreEditTip(open) {
  const tip = document.getElementById("ignore-edit-tip");
  const btn = document.getElementById("ignore-edit-tip-btn");
  if (!tip || !btn) return;
  tip.classList.toggle("is-open", open);
  if (open) tip.classList.remove("is-dismissed");
  btn.setAttribute("aria-expanded", String(open));
}

/**
 * Wire the "?" hint. Hover is pure CSS; everything else is here:
 *  - a click pins it open or closed (the only way on a touch screen);
 *  - keyboard focus opens it and leaving the button closes it.
 *    Only focus that matches :focus-visible counts, so a mouse click (which also focuses a button) is left to the click handler instead of being opened twice;
 *  - Escape closes it, even while the pointer still hovers it (WCAG 1.4.13: a hint must be dismissible without moving the pointer).
 *    CSS alone cannot do that, because the focused button keeps matching :focus-visible after any key press,
 *    so Escape also sets .is-dismissed, which beats :hover until the pointer leaves;
 *  - a click anywhere else closes it.
 * The document-level listeners are added once for the page's lifetime: this runs on every render of the Settings view,
 * and they look the elements up when the event fires, so they keep working after a re-render replaces the markup.
 */
function attachIgnoreEditTip() {
  const tip = document.getElementById("ignore-edit-tip");
  const btn = document.getElementById("ignore-edit-tip-btn");
  btn?.addEventListener("click", (e) => {
    e.stopPropagation();
    setIgnoreEditTip(!tip?.classList.contains("is-open"));
  });
  btn?.addEventListener("focus", () => {
    if (btn.matches(":focus-visible")) setIgnoreEditTip(true);
  });
  btn?.addEventListener("blur", () => setIgnoreEditTip(false));
  tip?.addEventListener("mouseleave", () =>
    tip.classList.remove("is-dismissed"),
  );

  if (ignoreEditTipBound) return;
  ignoreEditTipBound = true;
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    setIgnoreEditTip(false);
    document.getElementById("ignore-edit-tip")?.classList.add("is-dismissed");
  });
  document.addEventListener("click", (e) => {
    if (!(e.target instanceof Element) || !e.target.closest("#ignore-edit-tip"))
      setIgnoreEditTip(false);
  });
}

/**
 * Create the rule in the form (POST), or remove one (DELETE) - then reload so the screen shows what the server now holds.
 * A rejected request keeps the form exactly as it was, so a typo costs nothing.
 *
 * @param {HTMLElement} root
 * @param {{ remove?: string }} [opts] remove = the id of the rule to remove; omitted = add the form's rule
 */
async function changeIgnoreRules(root, { remove } = {}) {
  const s = settingsViewState.ignore;
  s.error = null;
  s.fix = null;
  s.notice = null;
  s.busy = remove ?? "add";
  renderRoot(root);

  let ok = false;
  try {
    const f = s.form;
    const res = remove
      ? await apiFetch(
          `/api/ingest/ignore-rules/${encodeURIComponent(remove)}`,
          { method: "DELETE" },
        )
      : await apiFetch("/api/ingest/ignore-rules", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            pattern: f.pattern,
            all_chats: f.scope === "all",
            // The form keeps ids as strings (checkbox values); the API wants numbers.
            // Telegram's marked chat ids (the largest, supergroups, are around -1e12) sit far inside the range a JS number holds exactly.
            chat_ids: f.scope === "chats" ? [...f.chatIds].map(Number) : [],
          }),
        });
    if (res.ok) ok = true;
    else await failIgnore(res);
  } catch {
    s.error = t("common.error");
  }
  s.busy = null;
  if (!ok) {
    renderRoot(root);
    return;
  }
  if (!remove)
    s.form = {
      pattern: "",
      scope: s.form.scope,
      chatIds: new Set(),
      filter: "",
    };
  s.notice = remove ? "removed" : "added";
  await loadIgnoreRules(root);
}

function attachIgnoreHandlers(root) {
  const s = settingsViewState.ignore;
  const f = s.form;
  attachIgnoreEditTip();
  const addButton = () => document.getElementById("ignore-add");
  // Enable/disable the button without re-rendering: a re-render would drop focus from the field being typed in.
  const syncAdd = () => {
    const btn = addButton();
    if (btn)
      btn.disabled = s.busy !== null || s.loading || !ignoreFormComplete();
  };

  const clearNotice = () => {
    s.notice = null;
    const el = document.getElementById("ignore-notice");
    if (el) {
      el.hidden = true;
      el.textContent = "";
    }
  };

  document.getElementById("ignore-pattern")?.addEventListener("input", (e) => {
    f.pattern = e.target.value;
    clearNotice();
    syncAdd();
  });

  // Show or hide the list of rules without a re-render, so the button keeps focus.
  document.getElementById("ignore-toggle")?.addEventListener("click", (e) => {
    s.showList = !s.showList;
    clearNotice();
    const list = document.getElementById("ignore-list");
    if (list) list.hidden = !s.showList;
    const button = /** @type {HTMLElement} */ (e.currentTarget);
    button.setAttribute("aria-expanded", String(s.showList));
    button.textContent = ignoreToggleLabel();
  });

  document.querySelectorAll('input[name="ignore-scope"]').forEach((radio) => {
    radio.addEventListener("change", (e) => {
      f.scope = e.target.value;
      const list = document.getElementById("ignore-chats");
      if (list) list.hidden = f.scope !== "chats";
      syncAdd();
    });
  });

  document
    .getElementById("ignore-chat-filter")
    ?.addEventListener("input", (e) => {
      f.filter = e.target.value;
      const needle = f.filter.trim().toLocaleLowerCase();
      document.querySelectorAll("[data-ignore-chat-name]").forEach((label) => {
        label.hidden =
          !!needle && !label.dataset.ignoreChatName.includes(needle);
      });
    });

  document.querySelectorAll("[data-ignore-chat]").forEach((box) => {
    box.addEventListener("change", (e) => {
      if (e.target.checked) f.chatIds.add(e.target.value);
      else f.chatIds.delete(e.target.value);
      syncAdd();
    });
  });

  document.getElementById("ignore-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    if (ignoreFormComplete() && s.busy === null && !s.loading)
      changeIgnoreRules(root);
  });

  document.querySelectorAll("[data-ignore-remove-id]").forEach((btn) => {
    btn.addEventListener("click", () =>
      changeIgnoreRules(root, { remove: btn.dataset.ignoreRemoveId }),
    );
  });
}

/**
 * Run one request against /api/telegram/sessions..., then reload the list.
 * Errors land in the card (describeSessionError: the archiver-is-running case gets its own wording) and never throw out of here.
 *
 * @param {HTMLElement} root
 * @param {{ method?: string, path?: string, busy?: string | null }} [opts] busy = what to mark as in progress (a device id, "others", or null for a plain load)
 */
async function callDevices(
  root,
  { method = "GET", path = "", busy = null } = {},
) {
  const s = settingsViewState.devices;
  s.error = null;
  if (busy === null) s.loading = true;
  else s.busy = busy;
  renderRoot(root);

  try {
    const res = await apiFetch(`/api/telegram/sessions${path}`, { method });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      s.error = describeSessionError(body.detail);
    } else if (method === "GET") {
      s.list = (await res.json()).devices;
    } else {
      // An end succeeded: fetch the list again so the screen shows what Telegram says now rather than what we assume.
      const list = await apiFetch("/api/telegram/sessions");
      if (list.ok) s.list = (await list.json()).devices;
      else
        s.error = describeSessionError(
          (await list.json().catch(() => ({}))).detail,
        );
    }
  } catch {
    s.error = t("common.error");
  }
  s.loading = false;
  s.busy = null;
  renderRoot(root);
}

// ---------------------------------------------------------------------------
// Automatic cleanup card (retention policy)
// ---------------------------------------------------------------------------

/** The months the form currently holds as a whole number within the server's limits, or null. */
function retentionMonths() {
  const s = settingsViewState.retention;
  const text = String(s.form.months).trim();
  if (!/^\d{1,3}$/.test(text)) return null;
  const n = Number(text);
  const min = s.policy?.min_months ?? 1;
  const max = s.policy?.max_months ?? 120;
  return n >= min && n <= max ? n : null;
}

/** True when the preview on screen was made with exactly the values in the form. */
function retentionPreviewFresh() {
  const s = settingsViewState.retention;
  const p = s.preview;
  return (
    p !== null &&
    p.months === retentionMonths() &&
    p.keepDeleted === s.form.keepDeleted
  );
}

function retentionStatusText() {
  const p = settingsViewState.retention.policy;
  if (!p || !p.configured || !p.enabled) return t("settings.retentionOff");
  return t("settings.retentionOn").replace("{months}", String(p.months));
}

function retentionLastRunText() {
  const p = settingsViewState.retention.policy;
  if (!p || !p.configured || !p.enabled) return "";
  if (!p.last_run_at) return t("settings.retentionNeverRan");
  const when = formatDate(p.last_run_at);
  if (p.last_run_status === "blocked")
    return t("settings.retentionBlocked").replace("{when}", when);
  if (p.last_run_status === "error")
    return t("settings.retentionFailed").replace("{when}", when);
  return t("settings.retentionLastRun")
    .replace("{when}", when)
    .replace("{n}", String(p.last_run_deleted ?? 0));
}

/** The preview result and the save button.
 *  Its own function so typing can swap it without re-rendering the form (focus stays in the field). */
function renderRetentionResult() {
  const s = settingsViewState.retention;
  const busy = s.busy !== null;
  const p = s.preview;
  if (!p || !retentionPreviewFresh()) return "";
  const saveLabel = s.policy?.enabled
    ? t("settings.retentionSave")
    : t("settings.retentionTurnOn");
  if (p.count === 0) {
    return `<p class="settings-help retention-result">${t("settings.retentionNothing")}</p>
      <button type="button" id="retention-save" class="modal__btn modal__btn--primary" ${busy ? "disabled" : ""}>${saveLabel}</button>`;
  }
  const text = t("settings.retentionWould")
    .replace("{count}", String(p.count))
    .replace("{total}", String(p.total))
    .replace("{from}", formatDate(p.oldest))
    .replace("{to}", formatDate(p.newest));
  return `<p class="settings-help retention-result" role="status">${escapeHtml(text)}</p>
    <button type="button" id="retention-save" class="modal__btn modal__btn--danger" ${busy ? "disabled" : ""}>${
      s.busy === "save" ? t("settings.retentionSaving") : saveLabel
    }</button>`;
}

function renderRetentionCard() {
  const s = settingsViewState.retention;
  const busy = s.busy !== null;

  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}${
        s.fix
          ? ` <a href="${s.fix.href}" class="settings-fix-link">${escapeHtml(s.fix.label)}</a>`
          : ""
      }</p>`
    : "";

  let body =
    s.loading && s.policy === null
      ? `<p class="settings-help">${t("common.loading")}</p>`
      : "";
  if (s.policy !== null) {
    const noticeKey = {
      saved: "settings.retentionSaved",
      off: "settings.retentionTurnedOff",
    }[s.notice];
    const lastRun = retentionLastRunText();
    const blocked = s.policy.enabled && s.policy.last_run_status === "blocked";
    const min = s.policy.min_months;
    const max = s.policy.max_months;
    body = `
      <p class="retention-status"><strong>${escapeHtml(retentionStatusText())}</strong></p>
      ${lastRun ? `<p class="${blocked ? "settings-error" : "settings-help"}" ${blocked ? 'role="alert"' : ""}>${escapeHtml(lastRun)}</p>` : ""}
      ${noticeKey ? `<p class="settings-help retention-notice" role="status">${t(noticeKey)}</p>` : ""}
      <form class="retention-form" id="retention-form" novalidate>
        <label class="settings-field">
          <span>${t("settings.retentionMonthsLabel")}</span>
          <input type="number" id="retention-months" min="${min}" max="${max}" step="1" inputmode="numeric" value="${escapeHtml(s.form.months)}" ${busy ? "disabled" : ""}>
        </label>
        <label class="retention-keep">
          <input type="checkbox" id="retention-keep" ${s.form.keepDeleted ? "checked" : ""} ${busy ? "disabled" : ""}>
          ${t("settings.retentionKeepDeleted")}
        </label>
        <button type="submit" id="retention-preview" class="modal__btn" ${busy || retentionMonths() === null ? "disabled" : ""}>
          ${s.busy === "preview" ? t("settings.retentionPreviewing") : t("settings.retentionPreview")}
        </button>
      </form>
      <div id="retention-result">${renderRetentionResult()}</div>
      ${
        s.policy.enabled
          ? `<div class="settings-actions"><button type="button" id="retention-off" class="modal__btn" ${busy ? "disabled" : ""}>${s.busy === "off" ? t("settings.retentionTurningOff") : t("settings.retentionTurnOff")}</button></div>`
          : ""
      }
    `;
  }

  return `
    <section class="settings-card settings-card--retention" id="retention-card">
      <h2>${t("settings.retentionTitle")}</h2>
      <p class="settings-help">${t("settings.retentionIntro")}</p>
      ${error}
      ${body}
    </section>
  `;
}

async function failRetention(res) {
  const s = settingsViewState.retention;
  const body = await res.json().catch(() => ({}));
  const detail = body.detail;
  if (detail && detail.reason === "preview_outdated") {
    // The archive changed between the preview and the save: show the new number and make the person look again.
    s.error = t("settings.retentionOutdated").replace(
      "{actual}",
      String(detail.actual),
    );
    s.fix = null;
    s.preview = null;
    return;
  }
  s.error = describeError(detail);
  s.fix = archiveFixLink(detail);
}

/** (Re)load the saved policy. Never throws; problems land in the card. Seeds the form from the policy the first time only. */
async function loadRetention(root) {
  const s = settingsViewState.retention;
  s.error = null;
  s.fix = null;
  s.loading = true;
  renderRoot(root);
  try {
    const res = await apiFetch("/api/archive/retention");
    if (!res.ok) {
      s.policy = null;
      await failRetention(res);
    } else {
      s.policy = await res.json();
      if (!s.seeded && s.policy.configured) {
        s.form = {
          months: String(s.policy.months),
          keepDeleted: s.policy.keep_deleted,
        };
        s.seeded = true;
      }
    }
  } catch {
    s.policy = null;
    s.error = t("common.error");
  }
  s.loading = false;
  renderRoot(root);
}

/** PUT the policy; on success reload it and show a one-line notice. */
async function saveRetention(root, payload, notice, busy) {
  const s = settingsViewState.retention;
  s.error = null;
  s.fix = null;
  s.notice = null;
  s.busy = busy;
  renderRoot(root);
  let ok = false;
  try {
    const res = await apiFetch("/api/archive/retention", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) ok = true;
    else await failRetention(res);
  } catch {
    s.error = t("common.error");
  }
  s.busy = null;
  if (!ok) {
    renderRoot(root);
    return;
  }
  s.preview = null;
  s.notice = notice;
  s.seeded = false;
  await loadRetention(root);
}

function attachRetentionHandlers(root) {
  const s = settingsViewState.retention;

  // Typing or ticking throws the preview away, in place: no re-render, so focus stays in the field.
  const invalidate = () => {
    s.notice = null;
    const result = document.getElementById("retention-result");
    if (result) result.innerHTML = renderRetentionResult();
    const btn = document.getElementById("retention-preview");
    if (btn) btn.disabled = s.busy !== null || retentionMonths() === null;
    attachRetentionSave(root);
  };
  document
    .getElementById("retention-months")
    ?.addEventListener("input", (e) => {
      s.form.months = e.target.value;
      invalidate();
    });
  document.getElementById("retention-keep")?.addEventListener("change", (e) => {
    s.form.keepDeleted = e.target.checked;
    invalidate();
  });

  document
    .getElementById("retention-form")
    ?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const months = retentionMonths();
      if (months === null || s.busy) return;
      const keepDeleted = s.form.keepDeleted;
      s.error = null;
      s.fix = null;
      s.notice = null;
      s.busy = "preview";
      renderRoot(root);
      try {
        const res = await apiFetch("/api/archive/retention/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ months, keep_deleted: keepDeleted }),
        });
        if (res.ok) {
          const data = await res.json();
          s.preview = {
            months,
            keepDeleted,
            count: data.count,
            total: data.total,
            oldest: data.oldest,
            newest: data.newest,
          };
        } else {
          s.preview = null;
          await failRetention(res);
        }
      } catch {
        s.preview = null;
        s.error = t("common.error");
      }
      s.busy = null;
      renderRoot(root);
    });

  attachRetentionSave(root);

  document.getElementById("retention-off")?.addEventListener("click", () => {
    const p = s.policy;
    if (!p || s.busy) return;
    saveRetention(
      root,
      { enabled: false, months: p.months, keep_deleted: p.keep_deleted },
      "off",
      "off",
    );
  });
}

/** The save button is re-created whenever the result area is, so its handler is (re)attached separately. */
function attachRetentionSave(root) {
  const s = settingsViewState.retention;
  document
    .getElementById("retention-save")
    ?.addEventListener("click", async () => {
      if (s.busy || !s.preview || !retentionPreviewFresh()) return;
      const p = s.preview;
      const message = t("settings.retentionConfirm")
        .replace("{count}", String(p.count))
        .replace("{months}", String(p.months));
      if (!(await showConfirm(message, { danger: true }))) return;
      saveRetention(
        root,
        {
          enabled: true,
          months: p.months,
          keep_deleted: p.keepDeleted,
          confirmed_messages: p.count,
        },
        "saved",
        "save",
      );
    });
}

/**
 * Clear everything stored.
 * The typed phrase (not just OK) is what keeps a stray click from emptying an archive;
 * the request carries its own fixed confirmation as well (api/routes/archive.py's CLEAR_CONFIRMATION), so a stray request cannot do it either.
 */
function attachDataHandlers(root) {
  document.getElementById("data-clear")?.addEventListener("click", async () => {
    const s = settingsViewState.data;
    if (s.busy) return;
    const phrase = t("settings.dataConfirmPhrase");
    if (
      !(await showTypedConfirm(t("settings.dataConfirm"), phrase, {
        title: t("settings.dataTitle"),
        confirmLabel: t("settings.dataClear"),
      }))
    )
      return;
    s.busy = true;
    s.error = null;
    renderRoot(root);
    try {
      const res = await apiFetch("/api/archive/data?confirm=delete-all-data", {
        method: "DELETE",
      });
      if (res.ok) {
        const done = await res.json();
        s.busy = false;
        renderRoot(root);
        // The chat view holds the old messages in memory (its quiet refresh only adds new ones), so it is told to drop them and leave the open chat.
        // The labels and the block rules are still there.
        window.dispatchEvent(new CustomEvent("televault:archive-cleared"));
        await showAlert(
          t("settings.dataCleared")
            .replace("{messages}", String(done.messages_deleted))
            .replace("{chats}", String(done.chats_deleted)),
        );
        return;
      }
      const body = await res.json().catch(() => ({}));
      s.error = describeError(body.detail);
    } catch {
      s.error = t("common.error");
    }
    s.busy = false;
    renderRoot(root);
  });
}

function attachDevicesHandlers(root) {
  document
    .getElementById("devices-load")
    ?.addEventListener("click", () => callDevices(root));

  document.querySelectorAll("[data-device-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.deviceId;
      const device = settingsViewState.devices.list?.find((d) => d.id === id);
      const message = t("settings.deviceEndConfirm").replace(
        "{device}",
        device ? deviceTitle(device) : "",
      );
      if (!(await showConfirm(message, { danger: true }))) return;
      // The id came from the server as a string of digits; encodeURIComponent is belt and braces for the URL.
      callDevices(root, {
        method: "DELETE",
        path: `/${encodeURIComponent(id)}`,
        busy: id,
      });
    });
  });

  document
    .getElementById("devices-end-others")
    ?.addEventListener("click", async () => {
      if (
        !(await showConfirm(t("settings.devicesEndOthersConfirm"), {
          danger: true,
        }))
      )
        return;
      callDevices(root, { method: "DELETE", busy: "others" });
    });
}

// ---------------------------------------------------------------------------
// Files card (file-type policy)
// ---------------------------------------------------------------------------

const FILE_KINDS = ["photo", "video", "voice", "audio", "document"];

/** The one-line confirmation after a save: always "Saved", plus what happened to files that were already waiting.
 *  @returns {string} */
function filesNoticeText() {
  const n = settingsViewState.files.notice;
  if (!n) return "";
  let text = t("settings.filesSaved");
  if (n.queued)
    text +=
      " " + t("settings.filesSavedQueued").replace("{n}", String(n.queued));
  if (n.skipped)
    text +=
      " " + t("settings.filesSavedSkipped").replace("{n}", String(n.skipped));
  return text;
}

/** Disk bar + counts + "Try again" for the files that were refused or failed.
 *  Empty until the usage numbers arrive. */
function renderFilesUsage() {
  const s = settingsViewState.files;
  const u = s.usage;
  if (!u) return "";
  const fill = (text, values) =>
    text.replace(/\{(\w+)\}/g, (_, k) => String(values[k] ?? ""));
  const percent =
    u.limit_bytes > 0
      ? Math.min(100, Math.round((u.stored_bytes / u.limit_bytes) * 100))
      : 0;
  const label = fill(t("settings.filesUsage"), {
    used: formatBytes(u.stored_bytes),
    limit: formatBytes(u.limit_bytes),
  });
  const refused =
    (u.counts.too_large || 0) +
    (u.counts.disk_full || 0) +
    (u.counts.failed || 0);
  const lines = [fill(t("settings.filesStored"), { n: u.stored_files })];
  if (u.counts.pending)
    lines.push(fill(t("settings.filesWaiting"), { n: u.counts.pending }));
  if (refused)
    lines.push(
      fill(t("settings.filesRefused"), {
        n: refused,
        max: formatBytes(u.max_file_bytes),
      }),
    );
  const retried =
    s.retried !== null
      ? `<p class="settings-help retention-notice" role="status">${escapeHtml(fill(t("settings.filesRetried"), { n: s.retried }))}</p>`
      : "";
  const retry = refused
    ? `<div class="settings-actions">
        <button type="button" id="files-retry" class="modal__btn" ${s.retrying ? "disabled" : ""}>${t(s.retrying ? "settings.filesRetrying" : "settings.filesRetry")}</button>
      </div>
      <p class="settings-help">${t("settings.filesRetryHelp")}</p>`
    : "";
  return `
    <div class="files-usage">
      <div class="files-usage__bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" aria-label="${escapeHtml(label)}">
        <div class="files-usage__fill" style="width:${percent}%"></div>
      </div>
      <p class="settings-help">${escapeHtml(label)}</p>
      <p class="settings-help">${escapeHtml(lines.join(" "))}</p>
      ${retried}
      ${retry}
    </div>`;
}

function renderFilesCard() {
  const s = settingsViewState.files;
  const error = s.error
    ? `<p class="settings-error" role="alert">${escapeHtml(s.error)}${
        s.fix
          ? ` <a href="${s.fix.href}" class="settings-fix-link">${escapeHtml(s.fix.label)}</a>`
          : ""
      }</p>`
    : "";
  let body =
    s.loading && s.policy === null
      ? `<p class="settings-help">${t("common.loading")}</p>`
      : "";
  if (s.policy !== null) {
    const notice = filesNoticeText();
    const none = FILE_KINDS.every((k) => !s.form[k]);
    body = `
      ${notice ? `<p class="settings-help retention-notice" role="status">${escapeHtml(notice)}</p>` : ""}
      <div class="files-kinds">
        ${FILE_KINDS.map(
          (k) => `<label class="retention-keep">
            <input type="checkbox" class="files-kind" data-kind="${k}" ${s.form[k] ? "checked" : ""} ${s.busy ? "disabled" : ""}>
            ${t("settings.filesKind." + k)}
          </label>`,
        ).join("")}
      </div>
      ${none ? `<p class="settings-help">${t("settings.filesNone")}</p>` : ""}
      ${renderFilesUsage()}
      <div class="settings-actions">
        <button type="button" id="files-save" class="modal__btn modal__btn--primary" ${s.busy ? "disabled" : ""}>
          ${s.busy ? t("settings.filesSaving") : t("settings.filesSave")}
        </button>
      </div>
    `;
  }
  return `
    <section class="settings-card settings-card--files" id="files-card">
      <h2>${t("settings.filesTitle")}</h2>
      <p class="settings-help">${t("settings.filesIntro")}</p>
      ${error}
      ${body}
    </section>
  `;
}

async function failFiles(res) {
  const s = settingsViewState.files;
  const body = await res.json().catch(() => ({}));
  s.error = describeError(body.detail);
  s.fix = archiveFixLink(body.detail);
}

/** (Re)load the saved choice into the ticks. Never throws; problems land in the card. @param {HTMLElement} root */
async function loadFiles(root) {
  const s = settingsViewState.files;
  s.error = null;
  s.fix = null;
  s.loading = true;
  renderRoot(root);
  try {
    const res = await apiFetch("/api/media/policy");
    if (!res.ok) {
      s.policy = null;
      await failFiles(res);
    } else {
      s.policy = await res.json();
      for (const k of FILE_KINDS) s.form[k] = !!s.policy[k];
    }
  } catch {
    s.policy = null;
    s.error = t("common.error");
  }
  s.loading = false;
  renderRoot(root);
  await loadFilesUsage(root);
}

/** The disk numbers are a nicety: a failure here just leaves the bar out (the ticks above keep working). */
async function loadFilesUsage(root) {
  const s = settingsViewState.files;
  try {
    const res = await apiFetch("/api/media/usage");
    s.usage = res.ok ? await res.json() : null;
  } catch {
    s.usage = null;
  }
  renderRoot(root);
}

function attachFilesHandlers(root) {
  const s = settingsViewState.files;
  // Ticking only edits the form; nothing is saved (or re-rendered, so focus stays) until "Save".
  root.querySelectorAll(".files-kind").forEach((box) =>
    box.addEventListener("change", (e) => {
      s.form[e.target.dataset.kind] = e.target.checked;
      s.notice = null;
      renderRoot(root);
      root
        .querySelector(`.files-kind[data-kind="${e.target.dataset.kind}"]`)
        ?.focus();
    }),
  );
  document
    .getElementById("files-retry")
    ?.addEventListener("click", async () => {
      if (s.retrying) return;
      s.retrying = true;
      s.retried = null;
      s.error = null;
      renderRoot(root);
      try {
        const res = await apiFetch("/api/media/retry", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (res.ok) s.retried = (await res.json()).requeued;
        else await failFiles(res);
      } catch {
        s.error = t("common.error");
      }
      s.retrying = false;
      await loadFilesUsage(root);
    });
  document.getElementById("files-save")?.addEventListener("click", async () => {
    if (s.busy) return;
    s.busy = true;
    s.error = null;
    s.fix = null;
    s.notice = null;
    renderRoot(root);
    try {
      const res = await apiFetch("/api/media/policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          Object.fromEntries(FILE_KINDS.map((k) => [k, s.form[k]])),
        ),
      });
      if (res.ok) {
        const data = await res.json();
        s.policy = Object.fromEntries(FILE_KINDS.map((k) => [k, !!data[k]]));
        s.notice = { skipped: data.skipped, queued: data.queued };
      } else {
        await failFiles(res);
      }
    } catch {
      s.error = t("common.error");
    }
    s.busy = false;
    renderRoot(root);
  });
}

function initSettingsView() {
  const root = document.getElementById("settings-root");
  if (!root) return;
  if (settingsViewState.initialized) return;
  settingsViewState.initialized = true;

  renderRoot(root);
  loadBlocklist(root);
  loadIgnoreRules(root);
  loadRetention(root);
  loadFiles(root);
}

export { initSettingsView };

// A chat was blocked or unblocked from the chat view while this page was already loaded: re-read the list instead of showing a stale one.
// Skipped while a request of this page is in flight - that request reloads the list itself, and its own event would otherwise cause a pointless second load.
document.addEventListener("televault:blocklistchanged", () => {
  if (!settingsViewState.initialized) return;
  const root = document.getElementById("settings-root");
  const s = settingsViewState.blocklist;
  if (root && !s.busy && !s.loading) loadBlocklist(root);
});

// Re-render in the new language - same pattern as the other views (e.g. health.js).
// The blocklist and the device list are redrawn from memory, not re-fetched.
// A server error message already shown stays in the language it was produced in until the next action;
// it is short-lived and re-translating it would need the raw `detail` kept around for no real gain.
document.addEventListener("televault:langchange", () => {
  if (!settingsViewState.initialized) return;
  const root = document.getElementById("settings-root");
  if (root) renderRoot(root);
});
