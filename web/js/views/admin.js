/**
 * Admin panel view - wires api/routes/admin.py into the UI: list every account, lock/unlock/delete them, and create/review invites.
 *
 * Lazy-initialized by app.js on first tab open, same pattern as the other non-landing views.
 * The nav item and this whole tab are hidden for non-admins (app.js, same isAdmin() check the wordmark's admin badge already uses) -
 * this file itself adds NO additional access check of its own, the same trust posture settings.js/backfill.js already take:
 * the real enforcement is server-side (api.dependencies.require_admin), client-side hiding is purely so a non-admin never sees a tab/button that would just 403 anyway.
 *
 * Deliberately two separate top-level fetches (users, invites) rather than one combined endpoint - they're independent lists an admin reads for different reasons,
 * and a failure in one (e.g. the invites list, which is secondary) shouldn't block rendering the other.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";
import { apiFetch, getCurrentUserId } from "../lib/auth.js";
import { describeError } from "../lib/errors.js";

const adminViewState = {
  initialized: false,
  /** Tagged union, same shape/reasoning as chatsViewState.lastRender (chats.js) - redraw() below branches on it. */
  usersRender: null, // { type: "data", users } | { type: "error", detail } | { type: "networkError" } | null
  invites: null, // GET /admin/invites' last result, or null if not loaded yet (shown as "loading" only briefly, never blocks the users card)
  busyUserId: null, // the one user row currently mid-action (lock/unlock/delete) - disables ITS buttons only, not the whole view
  inviteBusy: false,
  inviteExpiresHours: 24,
  inviteResult: null, // { token, expires_at } from the most recent successful create - shown once, see renderInvitesCard()
};

function formatDate(isoString) {
  if (!isoString) return "—";
  try {
    return new Date(isoString).toLocaleString();
  } catch {
    return isoString;
  }
}

/** Render adminViewState.usersRender's current value into the users card's body (not the whole view - see renderRoot()). */
function renderUsersCardBody() {
  const r = adminViewState.usersRender;
  if (!r) return `<div class="empty-state">${t("common.loading")}</div>`;
  if (r.type === "error")
    return `<div class="empty-state">${escapeHtml(describeError(r.detail))}</div>`;
  if (r.type === "networkError")
    return `<div class="empty-state">${t("common.error")}</div>`;

  const currentUserId = getCurrentUserId();
  const rowsHtml = r.users
    .map((user) => renderUserRow(user, currentUserId))
    .join("");
  return `<ul class="admin-user-list">${rowsHtml}</ul>`;
}

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
    // Single-admin model (Decisions Log) - there is no OTHER admin today,
    // but this branch is kept explicit (not collapsed into isSelf above) so the UI stays correct if that ever changes,
    // matching api/routes/admin.py's own "both conditions checked explicitly" reasoning for the same 400s server-side.
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

  return `
    <li class="admin-user-row">
      <div class="admin-user-row__body">
        <div class="admin-user-row__top">
          <span class="admin-user-row__name">${escapeHtml(user.username)}</span>
          ${badges}
        </div>
        <div class="admin-user-row__meta">
          <span>${user.has_telegram_session ? t("admin.tgLinked") : t("admin.tgNotLinked")}</span>
          <span>${user.has_archive ? t("admin.archiveSet") : t("admin.archiveMissing")}</span>
          <span>${t("admin.createdLabel")}: ${formatDate(user.created_at)}</span>
          <span>${t("admin.lastLoginLabel")}: ${formatDate(user.last_login_at)}</span>
        </div>
      </div>
      <div class="admin-user-row__actions">${actions}</div>
    </li>
  `;
}

function renderInviteRow(invite) {
  const status = invite.used_at
    ? `${t("admin.inviteUsedBy")}: ${escapeHtml(invite.used_by_username ?? t("admin.inviteUsedByDeleted"))}`
    : t("admin.inviteUnused");
  return `
    <li class="admin-invite-row">
      <span>${t("admin.inviteExpiresLabel")}: ${formatDate(invite.expires_at)}</span>
      <span>${status}</span>
    </li>
  `;
}

function renderInvitesCard() {
  const invites = adminViewState.invites;
  const listHtml =
    invites === null
      ? `<li class="settings-help">${t("common.loading")}</li>`
      : invites.length === 0
        ? `<li class="settings-help">${t("admin.noInvitesYet")}</li>`
        : invites.map(renderInviteRow).join("");

  // Shown once, right after a successful create - see api/schemas/admin.py's AdminInviteOut docstring for why a token can never be re-displayed later:
  // this is the ONLY place it's ever visible again after this render.
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
      <div class="settings-actions">
        <label class="modal__field">
          ${t("admin.expiresHoursLabel")}
          <input type="number" id="admin-invite-hours" min="1" max="720" value="${adminViewState.inviteExpiresHours}" />
        </label>
        <button type="button" id="admin-create-invite" class="modal__btn modal__btn--primary" ${adminViewState.inviteBusy ? "disabled" : ""}>
          ${adminViewState.inviteBusy ? t("admin.creatingInvite") : t("admin.createInviteButton")}
        </button>
      </div>
      ${resultHtml}
      <ul class="admin-invite-list">${listHtml}</ul>
    </section>
  `;
}

function renderRoot(root) {
  root.innerHTML = `
    <section class="settings-card settings-card--admin">
      <h2>${t("admin.usersCardTitle")}</h2>
      ${renderUsersCardBody()}
    </section>
    ${renderInvitesCard()}
  `;
  attachHandlers(root);
}

function attachHandlers(root) {
  root.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => handleUserAction(root, btn));
  });

  const createBtn = document.getElementById("admin-create-invite");
  if (createBtn)
    createBtn.addEventListener("click", () => handleCreateInvite(root));
}

async function handleUserAction(root, btn) {
  const userId = Number(btn.dataset.userId);
  const action = btn.dataset.action; // "lock" | "unlock" | "delete"

  if (action === "delete") {
    // Same "type the exact username back" confirmation as scripts/manage_admin.py's delete-admin subcommand -
    // deliberately stricter than lock/unlock's plain confirm() below, for the same reason that script gives:
    // this one is irreversible, and a single habitual "OK" is exactly the failure mode worth guarding against.
    const username = btn.dataset.username;
    const typed = window.prompt(
      t("admin.deleteConfirmPrompt").replace("{username}", username),
    );
    if (typed !== username) return;
  } else if (
    !window.confirm(
      t(action === "lock" ? "admin.lockConfirm" : "admin.unlockConfirm"),
    )
  ) {
    return;
  }

  adminViewState.busyUserId = userId;
  renderRoot(root);

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
      window.alert(describeError(body.detail));
      adminViewState.busyUserId = null;
      renderRoot(root);
      return;
    }
    adminViewState.busyUserId = null;
    await loadUsers(root); // re-fetch rather than patch the row in place - the simplest way to stay consistent with the server after any of the three actions
  } catch {
    window.alert(t("common.error"));
    adminViewState.busyUserId = null;
    renderRoot(root);
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
    if (!res.ok) {
      window.alert(describeError(body.detail));
      adminViewState.inviteBusy = false;
      renderRoot(root);
      return;
    }
    adminViewState.inviteBusy = false;
    adminViewState.inviteResult = body;
    renderRoot(root);
    await loadInvites(root);
  } catch {
    window.alert(t("common.error"));
    adminViewState.inviteBusy = false;
    renderRoot(root);
  }
}

async function loadUsers(root) {
  try {
    const res = await apiFetch("/api/admin/users");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      adminViewState.usersRender = { type: "error", detail: body.detail };
      renderRoot(root);
      return;
    }
    const data = await res.json();
    adminViewState.usersRender = { type: "data", users: data.users };
    renderRoot(root);
  } catch {
    adminViewState.usersRender = { type: "networkError" };
    renderRoot(root);
  }
}

async function loadInvites(root) {
  try {
    const res = await apiFetch("/api/admin/invites");
    if (!res.ok) return; // secondary list - leave it showing "loading"/stale rather than failing the whole view
    const data = await res.json();
    adminViewState.invites = data.invites;
    renderRoot(root);
  } catch {
    // Same - non-critical, no user-visible error for this one.
  }
}

/** Entry point called by app.js the first time the Admin tab is opened. */
function initAdminView() {
  const root = document.getElementById("admin-root");
  if (!root) return;
  if (adminViewState.initialized) return;
  adminViewState.initialized = true;

  renderRoot(root); // shows "loading" immediately
  loadUsers(root);
  loadInvites(root);
}

export { initAdminView };

// Re-render in the new language - no re-fetch needed, same reasoning as every other view's langchange listener.
document.addEventListener("televault:langchange", () => {
  if (!adminViewState.initialized) return;
  const root = document.getElementById("admin-root");
  if (root) renderRoot(root);
});
