/**
 * Backfill view.
 *
 * Lets the user trigger a historical-message backfill from the web UI (previously CLI-only), see its live progress, and review past runs.
 *
 * Polls GET /api/backfill/status every few seconds while a run is active - the backfill process is a separate subprocess from the API server,
 * so polling is the only way the UI learns about progress;
 * there is no push channel.
 *
 * Checks GET /api/telethon/status before allowing a start, since backfill.py and main.py cannot share the Telegram session at the same time.
 */

import { t } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";

const POLL_INTERVAL_MS = 3000;

const backfillViewState = {
  initialized: false,
  pollTimer: null,
  telethonRunning: null,
  modalOpen: false,
};

function formatDuration(seconds) {
  if (!seconds || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function renderDisclaimer() {
  return `
    <div class="backfill-disclaimer">
      <h2>${t("backfill.aboutTitle")}</h2>
      <ul>
        <li>${t("backfill.disclaimerSession")}</li>
        <li>${t("backfill.disclaimerDeleted")}</li>
        <li>${t("backfill.disclaimerEdits")}</li>
        <li>${t("backfill.disclaimerApprox")}</li>
        <li>${t("backfill.disclaimerBackground")}</li>
      </ul>
    </div>
  `;
}

function renderTelethonStatus() {
  const running = backfillViewState.telethonRunning;
  const dotClass =
    running === null
      ? ""
      : running
        ? "backfill-status-dot--on"
        : "backfill-status-dot--off";
  const label =
    running === null
      ? t("backfill.checkingConnection")
      : running
        ? t("backfill.connectionOn")
        : t("backfill.connectionOff");
  return `
    <div class="backfill-status-row">
      <span class="backfill-status-dot ${dotClass}"></span>
      <span>${label}</span>
    </div>
  `;
}

function renderProgress(status) {
  if (!status || status.state === "idle") return "";
  const percent =
    status.overall_total > 0
      ? Math.min(
          100,
          Math.round((status.overall_processed / status.overall_total) * 100),
        )
      : 0;
  const elapsed = status.started_at
    ? (Date.now() - new Date(status.started_at).getTime()) / 1000
    : 0;
  const eta =
    percent > 0 ? Math.round((elapsed / percent) * (100 - percent)) : null;
  const stateLabel =
    {
      running: t("backfill.stateRunning"),
      completed: t("backfill.stateCompleted"),
      cancelled: t("backfill.stateCancelled"),
      error: t("backfill.stateError"),
    }[status.state] ?? status.state;

  return `
    <div class="backfill-progress">
      <div class="backfill-progress__chat">${stateLabel}${status.current_chat ? ` — ${escapeHtml(status.current_chat)}` : ""}</div>
      <div class="progress-bar"><div class="progress-bar__fill" style="width: ${percent}%"></div></div>
      <div class="backfill-progress__meta">
        <span>${status.chats_done ?? 0}/${status.chats_total ?? "?"} ${t("backfill.chats")} · ${percent}%</span>
        <span>${eta !== null && status.state === "running" ? `${t("backfill.eta")}: ~${formatDuration(eta)}` : formatDuration(elapsed)}</span>
      </div>
      ${status.state === "running" ? `<button id="backfill-cancel-btn" class="backfill-cancel-btn">${t("backfill.cancel")}</button>` : ""}
    </div>
  `;
}

function renderHistory(history) {
  if (!history || history.length === 0)
    return `<div class="empty-state">${t("backfill.noHistory")}</div>`;
  const rows = history
    .map(
      (run) => `
    <tr>
      <td>${new Date(run.started_at).toLocaleString()}</td>
      <td>${run.status}</td>
      <td>${run.chats_done ?? 0}</td>
      <td>${run.messages_stored ?? 0}</td>
      <td>${run.messages_skipped ?? 0}</td>
      <td>${run.finished_at ? formatDuration((new Date(run.finished_at) - new Date(run.started_at)) / 1000) : "—"}</td>
    </tr>
  `,
    )
    .join("");
  return `
    <table class="backfill-history-table">
      <thead><tr>
        <th>${t("backfill.historyStarted")}</th><th>${t("backfill.historyStatus")}</th>
        <th>${t("backfill.historyChats")}</th><th>${t("backfill.historyStored")}</th>
        <th>${t("backfill.historySkipped")}</th><th>${t("backfill.historyDuration")}</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderModal() {
  const running = backfillViewState.telethonRunning;
  return `
    <div class="modal-overlay" id="backfill-modal-overlay">
      <div class="modal">
        <h2>${t("backfill.confirmTitle")}</h2>
        ${running ? `<div class="modal__warning">${t("backfill.warningConnectionOn")}</div>` : ""}
        <p>${t("backfill.confirmBody")}</p>
        <div class="modal__field">
          <label for="backfill-chat-input">${t("backfill.chatLabel")}</label>
          <input id="backfill-chat-input" type="text" placeholder="${t("backfill.chatPlaceholder")}" />
        </div>
        <div class="modal__field">
          <label for="backfill-limit-input">${t("backfill.limitLabel")}</label>
          <input id="backfill-limit-input" type="number" min="1" placeholder="${t("backfill.limitPlaceholder")}" />
        </div>
        <div class="modal__actions">
          <button class="modal__btn" id="backfill-modal-cancel">${t("common.cancel")}</button>
          <button class="modal__btn modal__btn--primary" id="backfill-modal-confirm" ${running ? "disabled" : ""}>
            ${t("backfill.confirmStart")}
          </button>
        </div>
      </div>
    </div>
  `;
}

async function fetchTelethonStatus() {
  try {
    const res = await fetch("/api/telethon/status");
    if (!res.ok) throw new Error();
    backfillViewState.telethonRunning = !!(await res.json()).running;
  } catch {
    backfillViewState.telethonRunning = null;
  }
}

async function fetchBackfillStatus() {
  try {
    const res = await fetch("/api/backfill/status");
    if (!res.ok) throw new Error();
    return await res.json();
  } catch {
    return { state: "idle" };
  }
}

async function fetchBackfillHistory() {
  try {
    const res = await fetch("/api/backfill/history");
    if (!res.ok) throw new Error();
    return await res.json();
  } catch {
    return [];
  }
}

async function openModal(root) {
  backfillViewState.modalOpen = true;
  await renderRoot(root);

  const overlay = document.getElementById("backfill-modal-overlay");
  document
    .getElementById("backfill-modal-cancel")
    .addEventListener("click", () => {
      backfillViewState.modalOpen = false;
      renderRoot(root);
    });
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) {
      backfillViewState.modalOpen = false;
      renderRoot(root);
    }
  });
  document
    .getElementById("backfill-modal-confirm")
    .addEventListener("click", async () => {
      const chat =
        document.getElementById("backfill-chat-input").value.trim() || null;
      const limit =
        document.getElementById("backfill-limit-input").value || null;
      try {
        await fetch("/api/backfill/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat, limit: limit ? Number(limit) : null }),
        });
      } catch {
        // The status poll below reflects whatever actually happened - no separate toast system needed.
      }
      backfillViewState.modalOpen = false;
      renderRoot(root);
      startPolling(root);
    });
}

async function renderRoot(root) {
  const [status, history] = await Promise.all([
    fetchBackfillStatus(),
    fetchBackfillHistory(),
  ]);

  root.innerHTML = `
    ${renderDisclaimer()}
    ${renderTelethonStatus()}
    <button id="backfill-start-btn" class="backfill-start-btn" ${status.state === "running" ? "disabled" : ""}>
      ${t("backfill.startButton")}
    </button>
    ${renderProgress(status)}
    <h2 class="stats-section-title">${t("backfill.historyTitle")}</h2>
    ${renderHistory(history)}
    ${backfillViewState.modalOpen ? renderModal() : ""}
  `;

  document
    .getElementById("backfill-start-btn")
    ?.addEventListener("click", async () => {
      await fetchTelethonStatus();
      await openModal(root);
    });
  document
    .getElementById("backfill-cancel-btn")
    ?.addEventListener("click", async () => {
      try {
        await fetch("/api/backfill/cancel", { method: "POST" });
      } catch {
        // Best-effort - next poll reflects reality either way.
      }
    });

  if (status.state === "running") startPolling(root);
  else if (backfillViewState.pollTimer) {
    clearInterval(backfillViewState.pollTimer);
    backfillViewState.pollTimer = null;
  }
}

function startPolling(root) {
  if (backfillViewState.pollTimer) return;
  backfillViewState.pollTimer = setInterval(
    () => renderRoot(root),
    POLL_INTERVAL_MS,
  );
}

function initBackfillView() {
  if (backfillViewState.initialized) return;
  backfillViewState.initialized = true;
  const root = document.getElementById("backfill-root");
  if (root) renderRoot(root);
}

export { initBackfillView };
