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

import { t, getCurrentLang } from "../i18n.js";
import { escapeHtml } from "../lib/dom.js";

const POLL_INTERVAL_MS = 3000;

// A run is only ever truly over once the status file says so explicitly.
// "idle" is NOT one of these - it's also what fetchBackfillStatus() returns on a network hiccup,
// and what the status file briefly still shows in the instant right after a start request,
// before the new subprocess has had a chance to write anything.
// Treating "idle" as terminal caused polling to die moments after starting a run,
// and the progress bar/Start button to look stale until the user manually reopened the modal.
const TERMINAL_STATES = new Set(["completed", "cancelled", "error"]);

const backfillViewState = {
  initialized: false,
  pollTimer: null,
  telethonRunning: null,
  modalOpen: false,
  historyFetchInFlight: false,
};

// This backend emits UTC timestamps in two shapes: SQLite's CURRENT_TIMESTAMP default ("YYYY-MM-DD HH:MM:SS", no offset - used for backfill_runs rows)
// and Python's isoformat() (now fixed to include a real +00:00 offset for the live status file,
// but older cached data or a not-yet-restarted process could still emit the old naive "YYYY-MM-DDTHH:MM:SS.ffffff" form).
// Neither naive form is safe to hand to `new Date()` directly: per the JS Date Time String spec,
// a date-time string with no timezone marker is parsed as LOCAL time, not UTC - silently shifting every duration/ETA calculation by a full timezone offset.
// If the string already carries an explicit offset, trust it as-is;
// otherwise normalize the separator and mark it UTC.
function parseUtc(value) {
  if (!value) return null;
  if (/[zZ]|[+-]\d{2}:\d{2}$/.test(value)) return new Date(value);
  return new Date(value.replace(" ", "T") + "Z");
}

function formatDuration(seconds) {
  if (!seconds || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const hUnit = t("backfill.unitHour");
  const mUnit = t("backfill.unitMinute");
  const sUnit = t("backfill.unitSecond");
  if (h > 0) return `${h}${hUnit} ${m}${mUnit}`;
  if (m > 0) return `${m}${mUnit} ${s}${sUnit}`;
  return `${s}${sUnit}`;
}

// toLocaleString() with no arguments uses the BROWSER's ambient locale, not the app's own EN/UK toggle - and without explicit field widths,
// the output length varies (e.g. a single-digit hour vs a double-digit one), which is what made history rows look inconsistently sized.
// Pinning both the locale and 2-digit widths for every field fixes both at once.
function formatDateTime(date) {
  if (!date) return "—";
  return date.toLocaleString(getCurrentLang(), {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function renderDisclaimer() {
  return `
    <div class="backfill-disclaimer">
      <h2>${t("backfill.aboutTitle")}</h2>
      <p class="backfill-disclaimer__intro">${t("backfill.aboutIntro")}</p>
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
  if (!status || status.state === "idle" || TERMINAL_STATES.has(status.state))
    return "";
  // Primary progress signal: chats completed, not messages processed.
  // overall_total can be dominated by a handful of large, mostly-already-archived chats on an incremental run,
  // which keeps a message-count percentage pinned near 0% for a long stretch even while real progress
  // (chats_done advancing) is happening - chats_done/chats_total doesn't have that problem, since every chat counts the same regardless of size.
  const chatsPercent =
    status.chats_total > 0
      ? Math.min(
          100,
          Math.round((status.chats_done / status.chats_total) * 100),
        )
      : 0;
  // Supplementary only: shown with real decimal precision rather than rounded to a whole percent,
  // since a legitimately-progressing run can sit at a fraction of a percent for a while (see above).
  const msgPercent =
    status.overall_total > 0
      ? Math.min(100, (status.overall_processed / status.overall_total) * 100)
      : 0;
  const elapsed = status.started_at
    ? (Date.now() - parseUtc(status.started_at).getTime()) / 1000
    : 0;
  const eta =
    chatsPercent > 0
      ? Math.round((elapsed / chatsPercent) * (100 - chatsPercent))
      : null;
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
      <div class="progress-bar"><div class="progress-bar__fill" style="width: ${chatsPercent}%"></div></div>
      <div class="backfill-progress__meta">
        <span>${status.chats_done ?? 0}/${status.chats_total ?? "?"} ${t("backfill.chats")} · ${chatsPercent}% · ${msgPercent.toFixed(1)}% ${t("backfill.byMessages")}</span>
        <span>${eta !== null && status.state === "running" ? `${t("backfill.eta")}: ~${formatDuration(eta)}` : formatDuration(elapsed)}</span>
      </div>
      ${status.state === "running" ? `<button id="backfill-cancel-btn" class="backfill-cancel-btn">${t("backfill.cancel")}</button>` : ""}
    </div>
  `;
}

function renderHistory(history) {
  if (!history || history.length === 0)
    return `<div class="empty-state">${t("backfill.noHistory")}</div>`;
  const stateLabels = {
    running: t("backfill.stateRunning"),
    completed: t("backfill.stateCompleted"),
    cancelled: t("backfill.stateCancelled"),
    error: t("backfill.stateError"),
  };
  const rows = history
    .map(
      (run) => `
    <tr>
      <td>${formatDateTime(parseUtc(run.started_at))}</td>
      <td>${stateLabels[run.status] ?? run.status}</td>
      <td>${run.chats_done ?? 0}</td>
      <td>${run.messages_stored ?? 0}</td>
      <td>${run.messages_skipped ?? 0}</td>
      <td>${run.finished_at ? formatDuration((parseUtc(run.finished_at) - parseUtc(run.started_at)) / 1000) : "—"}</td>
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
      startPolling(root);
      await renderRoot(root);
    });
}

async function renderRoot(root) {
  // Status and telethon-connection checks are fast, file-based reads.
  // History is DB-backed and fetched separately below, once the DOM below already exists - a slow history query (e.g. under heavy write load)
  // must never hold back the progress bar or Start button from updating, which is exactly what happened when all three were awaited together.
  const [status] = await Promise.all([
    fetchBackfillStatus(),
    fetchTelethonStatus(),
  ]);

  root.innerHTML = `
    ${renderDisclaimer()}
    ${renderTelethonStatus()}
    <button id="backfill-start-btn" class="backfill-start-btn" ${status.state === "running" ? "disabled" : ""}>
      ${t("backfill.startButton")}
    </button>
    ${renderProgress(status)}
    <h2 class="stats-section-title backfill-history-title">${t("backfill.historyTitle")}</h2>
    <div id="backfill-history-root">${t("common.loading")}</div>
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

  if (status.state === "running") {
    startPolling(root);
  } else if (TERMINAL_STATES.has(status.state) && backfillViewState.pollTimer) {
    clearInterval(backfillViewState.pollTimer);
    backfillViewState.pollTimer = null;
  }
  // "idle": leave any existing poll timer alone - see TERMINAL_STATES' comment.

  // Guarded against overlap: if a previous call's history fetch is still in flight (it's the one query here that can genuinely be slow),
  // don't pile another one on top of an already-busy database.
  if (!backfillViewState.historyFetchInFlight) {
    backfillViewState.historyFetchInFlight = true;
    fetchBackfillHistory()
      .then((history) => {
        const historyRoot = document.getElementById("backfill-history-root");
        if (historyRoot) historyRoot.innerHTML = renderHistory(history);
      })
      .finally(() => {
        backfillViewState.historyFetchInFlight = false;
      });
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
  backfillViewState.initialized = true;
  const root = document.getElementById("backfill-root");
  if (root) renderRoot(root);
}

// Re-render in the new language. Routed through openModal() rather than a
// plain renderRoot() when the modal is open, since renderRoot() alone only
// paints the modal's markup - it doesn't attach its button listeners
// (openModal() does that right after rendering). Calling renderRoot()
// directly here would leave a freshly-relabeled modal with dead buttons.
document.addEventListener("televault:langchange", () => {
  if (!backfillViewState.initialized) return;
  const root = document.getElementById("backfill-root");
  if (!root) return;
  if (backfillViewState.modalOpen) openModal(root);
  else renderRoot(root);
});

export { initBackfillView };
