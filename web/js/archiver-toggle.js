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
import { describeError } from "./lib/errors.js";

const POLL_INTERVAL_MS = 15000;

// main.py takes a moment to actually connect to Telegram (or disconnect) after the start/stop request returns
// - a single status check immediately afterward often still shows the OLD state, which made the button look like it hadn't done anything.
// Poll more tightly for a short window right after a click instead of waiting for the next slow periodic tick.
const SETTLE_POLL_INTERVAL_MS = 1000;
const SETTLE_POLL_MAX_ATTEMPTS = 15;

const state = {
  // "unknown" until the first status fetch resolves - avoids flashing a wrong running/stopped label for a moment on page load.
  status: "unknown",
  busy: false,
};

/** @param {HTMLElement} button */
function render(button) {
  const label = button.querySelector(".archiver-toggle__label");

  if (state.busy) {
    label.textContent =
      state.status === "running"
        ? t("archiver.stopping")
        : t("archiver.starting");
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
  // Dot color comes from the [data-state] CSS rule, driven by button.dataset.state.
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

/**
 * Poll status repeatedly until it reflects the expected post-action state
 * (or we give up after SETTLE_POLL_MAX_ATTEMPTS)
 * - keeps the button in its "busy" state the whole time so the user sees continuous feedback instead of one premature check.
 * @param {HTMLElement} button
 * @param {"running"|"stopped"} expected
 */
async function settlePoll(button, expected) {
  for (let attempt = 0; attempt < SETTLE_POLL_MAX_ATTEMPTS; attempt++) {
    await new Promise((resolve) =>
      setTimeout(resolve, SETTLE_POLL_INTERVAL_MS),
    );
    await refreshStatus(button); // updates state.status, still renders busy since state.busy is still true
    if (state.status === expected) break;
  }
  state.busy = false;
  render(button);
}

/** @param {HTMLElement} button */
async function handleClick(button) {
  if (state.busy || state.status === "unknown") return;

  if (
    state.status === "running" &&
    !window.confirm(t("archiver.confirmStop"))
  ) {
    return;
  }

  const wasRunning = state.status === "running";
  state.busy = true;
  render(button);

  const endpoint = wasRunning ? "stop" : "start";
  let ok = true;
  try {
    const res = await fetch(`/api/telethon/${endpoint}`, { method: "POST" });
    if (!res.ok) {
      ok = false;
      const body = await res.json().catch(() => ({}));
      window.alert(describeError(body.detail));
    }
  } catch {
    ok = false;
    window.alert(t("common.error"));
  }

  if (ok) {
    await settlePoll(button, wasRunning ? "stopped" : "running");
  } else {
    state.busy = false;
    await refreshStatus(button);
  }
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
