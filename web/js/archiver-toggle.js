/**
 * Persistent archiver run/stop control, lives in the nav bar (see #archiver-toggle in index.html) so it's reachable from every view,
 * not scoped to a single tab like the other view modules.
 *
 * Talks to GET/POST /api/telethon/{status,start,stop} (see api/routes/telethon.py).
 * Polls status every POLL_INTERVAL_MS so the button reflects reality even when the archiver was started/stopped some other way
 * (cron, systemd, a manual `python main.py`, or a crash) - not just after this button's own clicks.
 *
 * Self-initializes on DOMContentLoaded, like theme.js - this file is imported for that side effect by app.js, it has no exported init() to call lazily.
 */

import { t } from "./i18n.js";

const POLL_INTERVAL_MS = 15000;

const state = {
  // "unknown" until the first status fetch resolves - avoids flashing a wrong running/stopped label for a moment on page load.
  status: "unknown",
  busy: false,
};

/** @param {HTMLElement} button */
function render(button) {
  const dot = button.querySelector(".archiver-toggle__dot");
  const label = button.querySelector(".archiver-toggle__label");

  if (state.busy) {
    label.textContent =
      state.status === "running" ? t("archiver.stopping") : t("archiver.starting");
    button.disabled = true;
    button.dataset.state = "busy";
    return;
  }

  button.disabled = state.status === "unknown";
  button.dataset.state = state.status;

  if (state.status === "running") {
    label.textContent = `${t("archiver.running")} · ${t("archiver.stopAction")}`;
  } else if (state.status === "stopped") {
    label.textContent = `${t("archiver.stopped")} · ${t("archiver.startAction")}`;
  } else {
    label.textContent = "";
  }
  void dot; // color comes from data-state in CSS, nothing to set here directly.
}

/** @param {HTMLElement} button */
async function refreshStatus(button) {
  try {
    const res = await fetch("/api/telethon/status");
    const data = await res.json();
    state.status = data.running ? "running" : "stopped";
  } catch {
    // Leave state.status as whatever it last was rather than flipping to "unknown" on a single flaky request - a transient fetch failure
    // shouldn't make a running archiver look unreachable.
  }
  render(button);
}

/** @param {HTMLElement} button */
async function handleClick(button) {
  if (state.busy || state.status === "unknown") return;

  if (state.status === "running" && !window.confirm(t("archiver.confirmStop"))) {
    return;
  }

  state.busy = true;
  render(button);

  const endpoint = state.status === "running" ? "stop" : "start";
  try {
    const res = await fetch(`/api/telethon/${endpoint}`, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail || `HTTP ${res.status}`);
    }
  } catch {
    window.alert(t("common.error"));
  }

  state.busy = false;
  // main.py takes a moment to connect (or to disconnect) after the request returns,
  // so the very next status check may still show the old state - that's fine, the next poll tick will catch up.
  await refreshStatus(button);
}

document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("archiver-toggle");
  if (!button) return;

  render(button);
  refreshStatus(button);
  setInterval(() => refreshStatus(button), POLL_INTERVAL_MS);
  button.addEventListener("click", () => handleClick(button));
});

document.addEventListener("televault:langchange", () => {
  const button = document.getElementById("archiver-toggle");
  if (button) render(button);
});
