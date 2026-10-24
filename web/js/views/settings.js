/**
 * Settings view.
 *
 * Three cards:
 *   1. A link out to the dedicated telegram-setup.html page (see that file's own module docstring for why the Telegram/archive wizard lives there instead of here).
 *   2. "Chats not archived": the ingestion blocklist (api/routes/ingest.py, policy in handlers/ingest_filter.py) -
 *      pick an already-archived chat to stop archiving it, or take one off the list.
 *      It is a cheap database read, so unlike the devices card it loads as soon as the tab opens.
 *      Only chats the archive already knows are offered: a new chat is always archived until you block it here.
 *   3. "Telegram devices": the account's Telegram sessions (the same list as Telegram's own Settings -> Devices)
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
import { showConfirm } from "../lib/dialog.js";

const settingsViewState = {
  initialized: false,
  blocklist: {
    // null = not loaded (yet, or the load failed - see `error`), otherwise the rules from GET /api/ingest/blocklist.
    items: null,
    // Every archived chat as {chat_id, name} (GET /api/chats/options) - what the picker offers, minus what is already blocked.
    chats: [],
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
        <div class="device-row__name">${escapeHtml(rule.name || t("settings.blocklistUnnamed"))}</div>
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
          `<option value="${escapeHtml(String(c.chat_id))}" ${String(c.chat_id) === selected ? "selected" : ""}>${escapeHtml(c.name || `ID ${c.chat_id}`)}</option>`,
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
    ${renderDevicesCard()}
  `;
  attachBlocklistHandlers(root);
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
    const [rules, chats] = await Promise.all([
      apiFetch("/api/ingest/blocklist"),
      apiFetch("/api/chats/options"),
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
}

export { initSettingsView };

// Re-render in the new language - same pattern as the other views (e.g. health.js).
// The blocklist and the device list are redrawn from memory, not re-fetched.
// A server error message already shown stays in the language it was produced in until the next action;
// it is short-lived and re-translating it would need the raw `detail` kept around for no real gain.
document.addEventListener("televault:langchange", () => {
  if (!settingsViewState.initialized) return;
  const root = document.getElementById("settings-root");
  if (root) renderRoot(root);
});
