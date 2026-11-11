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
    error: null,
    // {href, label} when the error is one the person can fix themselves (no archive database yet), else null.
    fix: null,
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

    body += s.items.length
      ? `<ul class="device-list">${s.items.map(renderBlockedRow).join("")}</ul>`
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
    ${renderDataCard()}
    ${renderDevicesCard()}
  `;
  attachBlocklistHandlers(root);
  attachIgnoreHandlers(root);
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
      renderRoot(root);
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
      (s.items.length
        ? `<ul class="device-list">${s.items.map(renderIgnoreRow).join("")}</ul>`
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

  document.getElementById("ignore-pattern")?.addEventListener("input", (e) => {
    f.pattern = e.target.value;
    syncAdd();
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

function initSettingsView() {
  const root = document.getElementById("settings-root");
  if (!root) return;
  if (settingsViewState.initialized) return;
  settingsViewState.initialized = true;

  renderRoot(root);
  loadBlocklist(root);
  loadIgnoreRules(root);
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
