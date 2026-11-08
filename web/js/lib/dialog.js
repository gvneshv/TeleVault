/**
 * In-app replacements for window.alert() / window.confirm() / window.prompt().
 *
 * Why not the native ones: browsers let the person tick "prevent this page from creating additional dialogs",
 * after which alert()/confirm() silently return immediately (confirm() returns false, prompt() returns null) -
 * a destructive-action guard that can be switched off by accident, with no visible sign it happened, is not a guard.
 * These render inside the page instead, so that can't happen, and they match the app's theme/language
 * (the native ones are styled by the OS/browser and can't be translated by us).
 *
 * All of them return a Promise, so callers `await` them where they used to call a blocking function:
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
 * @param {{ href: string, label: string }} [opts.link] - a "go and fix it" link shown under the message.
 *   Built with DOM APIs like everything else here, so `label` is never parsed as HTML; `href` must be one of our own paths, never a value taken from a server response.
 * @param {{ label: string }} [opts.checkbox] - an extra tick box (always unticked at first) under the message.
 *   With it the promise resolves to `{ confirmed, checked }` instead of a bare boolean, so a caller cannot read the box without also knowing the answer.
 * @param {{ label: string, value: string, maxLength: number, blankMessage: string }} [opts.field] - a free-text field (showFieldDialog).
 *   A blank (after trimming) value is refused inline with `blankMessage`; the promise then resolves to `{ action, value }`.
 * @param {{ label: string, danger?: boolean }} [opts.secondary] - a second action button next to the confirm one (only with `field`): resolves `action: "secondary"`.
 * @param {Node} [opts.content] - extra, already-built DOM shown between the message and the field (a read-only list, say). Never parsed as HTML.
 * @returns {Promise<boolean | { confirmed: boolean, checked: boolean } | { action: "confirm" | "secondary" | "cancel", value: string }>}
 *   true if confirmed, false if cancelled/dismissed; the object forms only when `checkbox` / `field` was given.
 */
function openDialog({
  message,
  title,
  confirmLabel,
  showCancel,
  danger = false,
  typed,
  link,
  checkbox,
  field,
  secondary,
  content,
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

    if (link) {
      const anchor = document.createElement("a");
      anchor.className = "settings-fix-link modal__link";
      anchor.href = link.href;
      anchor.textContent = link.label;
      dialog.append(anchor);
    }

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

    if (content) dialog.append(content);

    if (field) {
      const row = document.createElement("label");
      row.className = "modal__field";
      const labelText = document.createElement("span");
      labelText.textContent = field.label;
      input = document.createElement("input");
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.maxLength = field.maxLength;
      input.value = field.value;
      row.append(labelText, input);
      hint = document.createElement("p");
      hint.className = "modal__hint";
      hint.setAttribute("role", "alert");
      hint.hidden = true;
      dialog.append(row, hint);
    }

    /** @type {HTMLInputElement | null} */
    let optionBox = null;
    if (checkbox) {
      const row = document.createElement("label");
      row.className = "modal__check";
      optionBox = document.createElement("input");
      optionBox.type = "checkbox";
      const text = document.createElement("span");
      text.textContent = checkbox.label;
      row.append(optionBox, text);
      dialog.append(row);
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

    let secondaryBtn = null;
    if (secondary) {
      secondaryBtn = document.createElement("button");
      secondaryBtn.type = "button";
      secondaryBtn.className = `modal__btn${secondary.danger ? " modal__btn--danger" : ""}`;
      secondaryBtn.textContent = secondary.label;
      actions.append(secondaryBtn);
    }

    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = `modal__btn ${danger ? "modal__btn--danger" : "modal__btn--primary"}`;
    confirmBtn.textContent = confirmLabel;
    actions.append(confirmBtn);

    dialog.append(actions);
    overlay.append(dialog);

    function close(result, action) {
      document.removeEventListener("keydown", onKeydown, true);
      overlay.remove();
      // The element may have been removed from the page while the dialog was open (e.g. a list re-render) - focus() on a detached node is a harmless no-op.
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
      if (field) {
        // The value is only meaningful for a confirmed save; Cancel/Escape/backdrop and the secondary action never hand it back.
        resolve({
          action: action ?? (result ? "confirm" : "cancel"),
          value: result && input ? input.value.trim() : "",
        });
        return;
      }
      resolve(
        optionBox
          ? { confirmed: result, checked: result && optionBox.checked }
          : result,
      );
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
      if (field && input && hint) {
        const blank = input.value.trim() === "";
        if (attempted) {
          hint.hidden = !blank;
          hint.textContent = field.blankMessage;
          input.setAttribute("aria-invalid", String(blank));
        }
        return !blank;
      }
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
        const focusable = dialog.querySelectorAll("button, input, a[href]");
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
    secondaryBtn?.addEventListener("click", () => close(false, "secondary"));
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
 * @param {{ title?: string, link?: { href: string, label: string } }} [options]
 *   `link`: an optional "go and fix it" link under the message (see archiveFixLink() in lib/archive-error.js).
 * @returns {Promise<void>}
 */
async function showAlert(message, { title, link } = {}) {
  await openDialog({
    message,
    title,
    confirmLabel: t("dialog.ok"),
    showCancel: false,
    link,
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

/**
 * Confirmation with one extra, initially unticked option - e.g. "also stop blocking this chat" under a destructive action.
 * The tick only counts when the action is confirmed (`checked` is false after Cancel/Escape), so callers can use it without re-checking `confirmed`.
 *
 * @param {string} message
 * @param {{ title?: string, confirmLabel?: string, danger?: boolean, checkboxLabel: string }} options
 * @returns {Promise<{ confirmed: boolean, checked: boolean }>}
 */
function showConfirmWithCheckbox(
  message,
  { title, confirmLabel, danger = false, checkboxLabel },
) {
  return /** @type {Promise<{ confirmed: boolean, checked: boolean }>} */ (
    openDialog({
      message,
      title,
      confirmLabel: confirmLabel ?? t("dialog.confirm"),
      showCancel: true,
      danger,
      checkbox: { label: checkboxLabel },
    })
  );
}

/**
 * A dialog with one text field, a Save button and an optional second action - used to rename a sender (and remove the name again).
 * Resolves `{ action, value }`: "confirm" (value = the trimmed text, never blank), "secondary" (the extra button), or "cancel" (Escape, backdrop, Cancel).
 * The field's `maxLength` is the browser-side copy of a limit enforced by the server (utils/validation.py); the server is the one that decides.
 *
 * @param {string} message
 * @param {{ title?: string, confirmLabel?: string, fieldLabel: string, value?: string, maxLength: number, blankMessage: string,
 *           secondaryLabel?: string, secondaryDanger?: boolean, content?: Node }} options
 * @returns {Promise<{ action: "confirm" | "secondary" | "cancel", value: string }>}
 */
function showFieldDialog(
  message,
  {
    title,
    confirmLabel,
    fieldLabel,
    value = "",
    maxLength,
    blankMessage,
    secondaryLabel,
    secondaryDanger = false,
    content,
  },
) {
  return /** @type {Promise<{ action: "confirm" | "secondary" | "cancel", value: string }>} */ (
    openDialog({
      message,
      title,
      confirmLabel: confirmLabel ?? t("dialog.save"),
      showCancel: true,
      content,
      field: { label: fieldLabel, value, maxLength, blankMessage },
      secondary: secondaryLabel
        ? { label: secondaryLabel, danger: secondaryDanger }
        : undefined,
    })
  );
}

export {
  showAlert,
  showConfirm,
  showConfirmWithCheckbox,
  showTypedConfirm,
  showFieldDialog,
};
