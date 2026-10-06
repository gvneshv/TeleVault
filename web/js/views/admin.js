/**
 * Admin panel view - wires api/routes/admin.py into the UI: list every account, lock/unlock/delete them, and create/review/delete invites.
 *
 * Loaded lazily by app.js via dynamic import() the first time the Admin tab is opened - and that tab only exists for the admin account (app.js mountAdminTab()),
 * so this file adds NO access check of its own: the real enforcement is server-side (api.dependencies.require_admin).
 *
 * Deliberately two separate top-level fetches (users, invites) rather than one combined endpoint - they're independent lists an admin reads for different reasons,
 * and a failure in one (e.g. the invites list, which is secondary) shouldn't block rendering the other.
 *
 * Search and pagination are CLIENT-SIDE: both lists are already fetched in full (personal-scale instance - tens of accounts, a few hundred invites at most),
 * so filtering/slicing in the browser needs no extra API surface.
 * If an instance ever grows past that, this is the part to move server-side.
 *
 * Rendering is split in two layers on purpose:
 *   - renderRoot() builds both cards' skeletons (search boxes, invite-create controls) - called rarely;
 *   - renderUsersList() / renderInvitesList() redraw ONLY the list + pager inside a card.
 * Typing in a search box must not rebuild the box itself, or it would lose focus after every keystroke.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { apiFetch, getCurrentUserId } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";
import { showAlert, showConfirm, showTypedConfirm } from "../lib/dialog.js";
import {
  render as renderPagination,
  attach as attachPagination,
} from "../lib/pagination.js";

const USERS_PAGE_SIZE = 10;
const INVITES_PAGE_SIZE = 10;

const adminViewState = {
  /** Tagged union, same shape/reasoning as chatsViewState.lastRender (chats.js). */
  usersRender: null, // { type: "data", users } | { type: "error", detail } | { type: "networkError" } | null
  usersQuery: "",
  usersPage: 1,
  invites: null, // GET /admin/invites' last result, or null if not loaded yet
  invitesQuery: "",
  invitesPage: 1,
  busyUserId: null, // the one user row currently mid-action - disables ITS buttons only
  busyInviteId: null,
  inviteBusy: false,
  inviteExpiresHours: 24,
  inviteResult: null, // { token, expires_at } from the most recent successful create - shown once
};

function formatDate(isoString) {
  if (!isoString) return "—";
  try {
    return new Date(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

/** Case-insensitive "contains" match - the "part-name search" both lists use. */
function includesCI(haystack, needle) {
  return String(haystack ?? "")
    .toLowerCase()
    .includes(needle);
}

/** Slice `items` to the current page, clamping the page if the list shrank (search narrowed, an item was deleted). */
function paginate(items, page, pageSize) {
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const current = Math.min(Math.max(1, page), pages);
  return {
    pageItems: items.slice((current - 1) * pageSize, current * pageSize),
    current,
    pages,
  };
}

// ---------------------------------------------------------------------------
// users
// ---------------------------------------------------------------------------

function renderUserRow(user, currentUserId) {
  const isSelf = user.id === currentUserId;
  const badges = [
    user.is_admin
      ? `<span class="info-badge">${t("admin.roleAdmin")}</span>`
      : "",
    user.is_locked
      ? `<span class="info-badge admin-user-row__locked-badge">${t("admin.statusLocked")}</span>`
      : "",
  ]
    .filter(Boolean)
    .join(" ");

  const busy = adminViewState.busyUserId === user.id;
  let actions;
  if (isSelf) {
    actions = `<span class="settings-help">${t("admin.youLabel")}</span>`;
  } else if (user.is_admin) {
    // Single-admin model - no OTHER admin exists today, but kept explicit (not collapsed into isSelf) to mirror the server-side checks.
    actions = `<span class="settings-help">${t("admin.adminLabel")}</span>`;
  } else {
    const lockAction = user.is_locked ? "unlock" : "lock";
    const lockLabel = user.is_locked
      ? t("admin.unlockButton")
      : t("admin.lockButton");
    actions = `
      <div class="settings-actions">
        <button type="button" class="modal__btn" data-action="${lockAction}" data-user-id="${user.id}" ${busy ? "disabled" : ""}>${lockLabel}</button>
        <button type="button" class="modal__btn modal__btn--danger" data-action="delete" data-user-id="${user.id}" data-username="${escapeHtml(user.username)}" ${busy ? "disabled" : ""}>${t("admin.deleteButton")}</button>
      </div>
    `;
  }

  // One " · "-joined line instead of separate spans, so the facts read as a list rather than one run-on sentence.
  const meta = [
    user.has_telegram_session ? t("admin.tgLinked") : t("admin.tgNotLinked"),
    user.has_archive ? t("admin.archiveSet") : t("admin.archiveMissing"),
    `${t("admin.createdLabel")}: ${formatDate(user.created_at)}`,
    `${t("admin.lastLoginLabel")}: ${formatDate(user.last_login_at)}`,
  ]
    .map(escapeHtml)
    .join(" · ");

  return `
    <li class="admin-user-row">
      <div class="admin-user-row__body">
        <div class="admin-user-row__top">
          <span class="admin-user-row__name">${escapeHtml(user.username)}</span>
          ${badges}
        </div>
        <div class="admin-user-row__meta">${meta}</div>
      </div>
      <div class="admin-user-row__actions">${actions}</div>
    </li>
  `;
}

function renderUsersList(root) {
  const listEl = root.querySelector("#admin-users-list");
  const pagerEl = root.querySelector("#admin-users-pager");
  if (!listEl || !pagerEl) return;

  const r = adminViewState.usersRender;
  pagerEl.innerHTML = "";
  if (!r) {
    listEl.innerHTML = `<div class="empty-state">${t("common.loading")}</div>`;
    return;
  }
  if (r.type === "error") {
    listEl.innerHTML = `<div class="empty-state">${escapeHtml(describeError(r.detail))}</div>`;
    return;
  }
  if (r.type === "networkError") {
    listEl.innerHTML = `<div class="empty-state">${t("common.error")}</div>`;
    return;
  }

  const needle = adminViewState.usersQuery.trim().toLowerCase();
  const filtered = needle
    ? r.users.filter((u) => includesCI(u.username, needle))
    : r.users;
  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="empty-state">${t("admin.noMatches")}</div>`;
    return;
  }

  const { pageItems, current, pages } = paginate(
    filtered,
    adminViewState.usersPage,
    USERS_PAGE_SIZE,
  );
  adminViewState.usersPage = current;
  const currentUserId = getCurrentUserId();
  listEl.innerHTML = `<ul class="admin-user-list">${pageItems.map((u) => renderUserRow(u, currentUserId)).join("")}</ul>`;
  listEl.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => handleUserAction(root, btn));
  });

  pagerEl.innerHTML = renderPagination(current, pages);
  attachPagination(pagerEl, current, pages, (page) => {
    adminViewState.usersPage = page;
    renderUsersList(root);
  });
}

async function handleUserAction(root, btn) {
  const userId = Number(btn.dataset.userId);
  const action = btn.dataset.action; // "lock" | "unlock" | "delete"

  if (action === "delete") {
    // Typed confirmation (lib/dialog.js) - same "type the exact username back" guard as scripts/manage_admin.py's delete-admin:
    // irreversible, and a single habitual "OK" is exactly the failure mode worth guarding against.
    const username = btn.dataset.username;
    const ok = await showTypedConfirm(
      t("admin.deleteConfirmMessage"),
      username,
      {
        title: t("admin.deleteConfirmTitle"),
        confirmLabel: t("admin.deleteButton"),
      },
    );
    if (!ok) return;
  } else {
    const ok = await showConfirm(
      t(action === "lock" ? "admin.lockConfirm" : "admin.unlockConfirm"),
      {
        confirmLabel: t(
          action === "lock" ? "admin.lockButton" : "admin.unlockButton",
        ),
      },
    );
    if (!ok) return;
  }

  adminViewState.busyUserId = userId;
  renderUsersList(root);

  try {
    const path =
      action === "delete"
        ? `/api/admin/users/${userId}`
        : `/api/admin/users/${userId}/${action}`;
    const res = await apiFetch(path, {
      method: action === "delete" ? "DELETE" : "POST",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      adminViewState.busyUserId = null;
      renderUsersList(root);
      await showAlert(describeError(body.detail));
      return;
    }
    adminViewState.busyUserId = null;
    await loadUsers(root); // re-fetch rather than patch in place - simplest way to stay consistent with the server
    // A deleted user's invite now shows "(account since deleted)" - refresh that list too.
    if (action === "delete") await loadInvites(root);
  } catch {
    adminViewState.busyUserId = null;
    renderUsersList(root);
    await showAlert(t("common.error"));
  }
}

// ---------------------------------------------------------------------------
// invites
// ---------------------------------------------------------------------------

/** "used" | "expired" | "active".
 * Computed in the browser from expires_at - see the module docstring on why that's fine at this scale. */
function inviteStatus(invite) {
  if (invite.used_at) return "used";
  return new Date(invite.expires_at).getTime() <= Date.now()
    ? "expired"
    : "active";
}

function renderInviteRow(invite) {
  const status = inviteStatus(invite);
  const busy = adminViewState.busyInviteId === invite.id;

  let detail;
  if (status === "used") {
    const who = invite.used_by_username ?? t("admin.inviteUsedByDeleted");
    detail = `${t("admin.inviteUsedBy")}: ${who} · ${formatDate(invite.used_at)}`;
  } else if (status === "expired") {
    detail = `${t("admin.inviteExpiredOnLabel")}: ${formatDate(invite.expires_at)}`;
  } else {
    detail = `${t("admin.inviteUnused")} · ${t("admin.inviteExpiresLabel")}: ${formatDate(invite.expires_at)}`;
  }

  const statusLabel = t(
    `admin.inviteStatus${status[0].toUpperCase()}${status.slice(1)}`,
  );
  // Only unused invites can be deleted (server enforces it too, 409) - a used one is the record of who registered with it.
  const deleteBtn =
    status === "used"
      ? ""
      : `<button type="button" class="modal__btn modal__btn--danger admin-btn--sm" data-invite-id="${invite.id}" ${busy ? "disabled" : ""}>${t("admin.inviteDeleteButton")}</button>`;

  return `
    <li class="admin-invite-row">
      <div class="admin-invite-row__main">
        <span class="admin-invite-row__id">#${invite.id}</span>
        <span class="info-badge admin-status--${status}">${statusLabel}</span>
        <span>${escapeHtml(detail)}</span>
      </div>
      ${deleteBtn}
    </li>
  `;
}

function renderInvitesList(root) {
  const listEl = root.querySelector("#admin-invites-list");
  const pagerEl = root.querySelector("#admin-invites-pager");
  if (!listEl || !pagerEl) return;

  pagerEl.innerHTML = "";
  const invites = adminViewState.invites;
  if (invites === null) {
    listEl.innerHTML = `<p class="settings-help">${t("common.loading")}</p>`;
    return;
  }
  if (invites.length === 0) {
    listEl.innerHTML = `<p class="settings-help">${t("admin.noInvitesYet")}</p>`;
    return;
  }

  // Matches the redeemer's name (part-name, case-insensitive) or the invite's #id.
  // NOT the token itself: the API never returns past tokens (see api/schemas/admin.py), so there is nothing here to search.
  const needle = adminViewState.invitesQuery
    .trim()
    .toLowerCase()
    .replace(/^#/, "");
  const filtered = needle
    ? invites.filter(
        (i) =>
          includesCI(i.used_by_username, needle) ||
          String(i.id).includes(needle),
      )
    : invites;
  if (filtered.length === 0) {
    listEl.innerHTML = `<p class="settings-help">${t("admin.noMatches")}</p>`;
    return;
  }

  const { pageItems, current, pages } = paginate(
    filtered,
    adminViewState.invitesPage,
    INVITES_PAGE_SIZE,
  );
  adminViewState.invitesPage = current;
  listEl.innerHTML = `<ul class="admin-invite-list">${pageItems.map(renderInviteRow).join("")}</ul>`;
  listEl.querySelectorAll("[data-invite-id]").forEach((btn) => {
    btn.addEventListener("click", () =>
      handleInviteDelete(root, Number(btn.dataset.inviteId)),
    );
  });

  pagerEl.innerHTML = renderPagination(current, pages);
  attachPagination(pagerEl, current, pages, (page) => {
    adminViewState.invitesPage = page;
    renderInvitesList(root);
  });
}

async function handleInviteDelete(root, inviteId) {
  const ok = await showConfirm(t("admin.inviteDeleteConfirm"), {
    danger: true,
    confirmLabel: t("admin.inviteDeleteButton"),
  });
  if (!ok) return;

  adminViewState.busyInviteId = inviteId;
  renderInvitesList(root);
  try {
    const res = await apiFetch(`/api/admin/invites/${inviteId}`, {
      method: "DELETE",
    });
    adminViewState.busyInviteId = null;
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      renderInvitesList(root);
      await showAlert(describeError(body.detail));
      // A 404/409 means our copy is stale (deleted/used elsewhere) - refresh so the row reflects reality.
      await loadInvites(root);
      return;
    }
    await loadInvites(root);
  } catch {
    adminViewState.busyInviteId = null;
    renderInvitesList(root);
    await showAlert(t("common.error"));
  }
}

async function handleCreateInvite(root) {
  const hoursInput = document.getElementById("admin-invite-hours");
  const hours = Math.min(720, Math.max(1, Number(hoursInput?.value) || 24));
  adminViewState.inviteExpiresHours = hours;
  adminViewState.inviteBusy = true;
  adminViewState.inviteResult = null;
  renderRoot(root);

  try {
    const res = await apiFetch("/api/admin/invites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expires_hours: hours }),
    });
    const body = await res.json().catch(() => ({}));
    adminViewState.inviteBusy = false;
    if (!res.ok) {
      renderRoot(root);
      await showAlert(describeError(body.detail));
      return;
    }
    adminViewState.inviteResult = body;
    renderRoot(root);
    await loadInvites(root);
  } catch {
    adminViewState.inviteBusy = false;
    renderRoot(root);
    await showAlert(t("common.error"));
  }
}

// ---------------------------------------------------------------------------
// skeleton + loading
// ---------------------------------------------------------------------------

function renderInvitesCardShell() {
  // Shown once, right after a successful create - see api/schemas/admin.py's AdminInviteOut docstring for why a token can never be re-displayed later.
  const resultHtml = adminViewState.inviteResult
    ? `
      <div class="admin-invite-result">
        <p class="settings-help">${t("admin.inviteCreatedLabel")}</p>
        <code class="admin-invite-token">${escapeHtml(adminViewState.inviteResult.token)}</code>
        <p class="settings-help">${t("admin.inviteExpiresLabel")}: ${formatDate(adminViewState.inviteResult.expires_at)}</p>
      </div>
    `
    : "";

  return `
    <section class="settings-card settings-card--admin">
      <h2>${t("admin.invitesCardTitle")}</h2>
      <p class="settings-help">${t("admin.invitesCardBody")}</p>
      <div class="admin-invite-controls">
        <label class="modal__field">
          ${t("admin.expiresHoursLabel")}
          <input type="number" id="admin-invite-hours" min="1" max="720" value="${adminViewState.inviteExpiresHours}" />
        </label>
        <button type="button" id="admin-create-invite" class="modal__btn modal__btn--primary" ${adminViewState.inviteBusy ? "disabled" : ""}>
          ${adminViewState.inviteBusy ? t("admin.creatingInvite") : t("admin.createInviteButton")}
        </button>
      </div>
      ${resultHtml}
      <div class="admin-search">
        <input type="search" id="admin-invites-search" placeholder="${escapeHtml(t("admin.searchInvitesPlaceholder"))}" value="${escapeHtml(adminViewState.invitesQuery)}" autocomplete="off" />
      </div>
      <div id="admin-invites-list"></div>
      <div id="admin-invites-pager"></div>
    </section>
  `;
}

function renderRoot(root) {
  root.innerHTML = `
    <section class="settings-card settings-card--admin">
      <h2>${t("admin.usersCardTitle")}</h2>
      <div class="admin-search">
        <input type="search" id="admin-users-search" placeholder="${escapeHtml(t("admin.searchUsersPlaceholder"))}" value="${escapeHtml(adminViewState.usersQuery)}" autocomplete="off" />
      </div>
      <div id="admin-users-list"></div>
      <div id="admin-users-pager"></div>
    </section>
    ${renderInvitesCardShell()}
  `;

  root.querySelector("#admin-users-search").addEventListener("input", (e) => {
    adminViewState.usersQuery = e.target.value;
    adminViewState.usersPage = 1;
    renderUsersList(root);
  });
  root.querySelector("#admin-invites-search").addEventListener("input", (e) => {
    adminViewState.invitesQuery = e.target.value;
    adminViewState.invitesPage = 1;
    renderInvitesList(root);
  });
  root
    .querySelector("#admin-create-invite")
    .addEventListener("click", () => handleCreateInvite(root));

  renderUsersList(root);
  renderInvitesList(root);
}

async function loadUsers(root) {
  try {
    const res = await apiFetch("/api/admin/users");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      adminViewState.usersRender = { type: "error", detail: body.detail };
    } else {
      const data = await res.json();
      adminViewState.usersRender = { type: "data", users: data.users };
    }
  } catch {
    adminViewState.usersRender = { type: "networkError" };
  }
  renderUsersList(root);
}

async function loadInvites(root) {
  try {
    const res = await apiFetch("/api/admin/invites");
    if (!res.ok) return; // secondary list - leave it showing "loading"/stale rather than failing the whole view
    const data = await res.json();
    adminViewState.invites = data.invites;
    renderInvitesList(root);
  } catch {
    // Same - non-critical, no user-visible error for this one.
  }
}

/**
 * Entry point called by app.js the first time the Admin tab is opened.
 * "Already initialized" is tracked on the root ELEMENT (dataset), not in module state:
 * app.js can unmount/remount the tab's DOM, and a fresh element must render again.
 */
function initAdminView() {
  const root = document.getElementById("admin-root");
  if (!root || root.dataset.adminInit === "1") return;
  root.dataset.adminInit = "1";

  renderRoot(root); // shows "loading" immediately
  loadUsers(root);
  loadInvites(root);
}

export { initAdminView };

// Re-render in the new language - no re-fetch needed, same reasoning as every other view's langchange listener.
document.addEventListener("televault:langchange", () => {
  const root = document.getElementById("admin-root");
  if (root && root.dataset.adminInit === "1") renderRoot(root);
});
