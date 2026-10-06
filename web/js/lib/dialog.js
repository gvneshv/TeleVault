/**
 * In-app replacements for window.alert() / window.confirm() / window.prompt().
 *
 * Why not the native ones: browsers let the person tick "prevent this page from creating additional dialogs",
 * after which alert()/confirm() silently return immediately (confirm() returns false, prompt() returns null) -
 * a destructive-action guard that can be switched off by accident, with no visible sign it happened, is not a guard.
 * These render inside the page instead, so that can't happen, and they match the app's theme/language
 * (the native ones are styled by the OS/browser and can't be translated by us).
 *
 * All three return a Promise, so callers `await` them where they used to call a blocking function:
 *
 *     if (!(await showConfirm(t("archiver.confirmStop")))) return;
 *
 * Built with DOM APIs and textContent (never innerHTML), so callers can pass any string - a username, a backend error message - without escaping it first.
 *
 * Reuses the existing .modal-overlay / .modal / .modal__btn styles (see base.css) that the Backfill modal already uses.
 *
 * Accessibility, kept deliberately small but real: role="dialog"/"alertdialog" + aria-modal, focus moves into the dialog and returns to whatever had it before,
 * Tab is trapped inside, Escape and a click on the backdrop dismiss (= cancel).
 */

import { t } from "../i18n.js";

/**
 * Shared engine behind the three public functions.
 *
 * @param {object} opts
 * @param {string} opts.message
 * @param {string} [opts.title]
 * @param {string} opts.confirmLabel
 * @param {boolean} opts.showCancel - false for alerts (a single OK button).
 * @param {boolean} [opts.danger] - outlined seal-colored confirm button instead of the filled primary one;
 *   also moves initial focus to Cancel, so a reflexive Enter/Space never lands on the destructive choice.
 * @param {{ expected: string, label: string }} [opts.typed] - require the person to type `expected` exactly before confirming.
 * @returns {Promise<boolean>} true if confirmed, false if cancelled/dismissed.
 */
function openDialog({
  message,
  title,
  confirmLabel,
  showCancel,
  danger = false,
  typed,
}) {
  return new Promise((resolve) => {
    const previouslyFocused = document.activeElement;

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    const dialog = document.createElement("div");
    dialog.className = "modal";
    dialog.setAttribute("role", showCancel ? "dialog" : "alertdialog");
    dialog.setAttribute("aria-modal", "true");

    if (title) {
      const h2 = document.createElement("h2");
      h2.id = `dialog-title-${Date.now()}`;
      h2.textContent = title;
      dialog.setAttribute("aria-labelledby", h2.id);
      dialog.append(h2);
    }

    const messageEl = document.createElement("p");
    messageEl.className = "modal__message";
    messageEl.id = `dialog-message-${Date.now()}`;
    messageEl.textContent = message;
    dialog.setAttribute("aria-describedby", messageEl.id);
    dialog.append(messageEl);

    /** @type {HTMLInputElement | null} */
    let input = null;
    /** @type {HTMLElement | null} */
    let hint = null;
    if (typed) {
      const field = document.createElement("label");
      field.className = "modal__field";
      const labelText = document.createElement("span");
      labelText.textContent = typed.label;
      input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      field.append(labelText, input);

      // Hidden until a confirm attempt fails (or the person keeps typing after one has) - see validate() below.
      hint = document.createElement("p");
      hint.className = "modal__hint";
      hint.setAttribute("role", "alert");
      hint.hidden = true;

      dialog.append(field, hint);
    }

    const actions = document.createElement("div");
    actions.className = "modal__actions";

    let cancelBtn = null;
    if (showCancel) {
      cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "modal__btn";
      cancelBtn.textContent = t("common.cancel");
      actions.append(cancelBtn);
    }

    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = `modal__btn ${danger ? "modal__btn--danger" : "modal__btn--primary"}`;
    confirmBtn.textContent = confirmLabel;
    actions.append(confirmBtn);

    dialog.append(actions);
    overlay.append(dialog);

    function close(result) {
      document.removeEventListener("keydown", onKeydown, true);
      overlay.remove();
      // The element may have been removed from the page while the dialog was open (e.g. a list re-render) - focus() on a detached node is a harmless no-op.
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
      resolve(result);
    }

    /**
     * Typed-confirmation check.
     * The button stays clickable rather than disabled-until-match:
     * a silently dead button gives no hint about WHY nothing is happening, which is exactly the "nothing reacts" problem this replaced.
     * Instead an attempt with the wrong (or blank) text explains itself inline, and the message then tracks further typing live.
     * Surrounding whitespace is ignored (a pasted name often carries a trailing space);
     * case is NOT - usernames are case-sensitive.
     */
    let attempted = false;
    function validate() {
      if (!typed || !input || !hint) return true;
      const value = input.value.trim();
      const ok = value === typed.expected;
      if (attempted) {
        hint.hidden = ok;
        hint.textContent =
          value === "" ? t("dialog.typedBlank") : t("dialog.typedMismatch");
        input.setAttribute("aria-invalid", String(!ok));
      }
      return ok;
    }

    function tryConfirm() {
      attempted = true;
      if (validate()) {
        close(true);
      } else {
        input?.focus();
      }
    }

    function onKeydown(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(false);
        return;
      }
      if (event.key === "Enter" && input && event.target === input) {
        event.preventDefault();
        tryConfirm();
        return;
      }
      if (event.key === "Tab") {
        // Minimal focus trap: cycle within the dialog instead of letting Tab wander onto the page behind the backdrop.
        const focusable = dialog.querySelectorAll("button, input");
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    confirmBtn.addEventListener("click", tryConfirm);
    cancelBtn?.addEventListener("click", () => close(false));
    input?.addEventListener("input", validate);
    // mousedown (not click) on the backdrop itself: a text selection that starts inside the dialog and is released outside it fires click on the overlay too,
    // and must not be mistaken for a deliberate dismiss.
    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) close(false);
    });
    document.addEventListener("keydown", onKeydown, true);

    document.body.append(overlay);
    (input ?? (danger ? cancelBtn : confirmBtn) ?? confirmBtn).focus();
  });
}

/**
 * Replacement for window.alert().
 * Resolves when dismissed.
 * @param {string} message
 * @param {{ title?: string }} [options]
 * @returns {Promise<void>}
 */
async function showAlert(message, { title } = {}) {
  await openDialog({
    message,
    title,
    confirmLabel: t("dialog.ok"),
    showCancel: false,
  });
}

/**
 * Replacement for window.confirm().
 * Resolves true on confirm, false on cancel/Escape/backdrop click.
 * @param {string} message
 * @param {{ title?: string, confirmLabel?: string, danger?: boolean }} [options]
 * @returns {Promise<boolean>}
 */
function showConfirm(message, { title, confirmLabel, danger = false } = {}) {
  return openDialog({
    message,
    title,
    confirmLabel: confirmLabel ?? t("dialog.confirm"),
    showCancel: true,
    danger,
  });
}

/**
 * "Type this exact text to proceed" confirmation, for irreversible actions (replaces window.prompt() + an equality check).
 * Resolves true only if the person typed `expected` exactly; wrong/blank input shows an inline explanation instead of silently doing nothing.
 *
 * @param {string} message
 * @param {string} expected - the text that must be typed back (e.g. the username being deleted).
 * @param {{ title?: string, confirmLabel?: string }} [options]
 * @returns {Promise<boolean>}
 */
function showTypedConfirm(message, expected, { title, confirmLabel } = {}) {
  return openDialog({
    message,
    title,
    confirmLabel: confirmLabel ?? t("dialog.confirm"),
    showCancel: true,
    danger: true,
    typed: {
      expected,
      label: t("dialog.typedInputLabel").replace("{value}", expected),
    },
  });
}

export { showAlert, showConfirm, showTypedConfirm };
