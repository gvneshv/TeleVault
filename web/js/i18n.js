/**
 * Minimal i18n helper.
 *
 * Translation tables are imported directly from js/i18n/en.js and js/i18n/uk.js (ES modules) — see the TRANSLATIONS table below.
 * No bundler, no fetch: this keeps the app usable offline (relevant once the service worker caches it) and avoids a flash of untranslated content
 * while a JSON file loads.
 *
 * Adding a new language later: create js/i18n/<code>.js exporting a named object (following en.js/uk.js's pattern), import it below,
 * add it to TRANSLATIONS and SUPPORTED_LANGS, and add an <option> in the language <select>.
 */

import { en } from "./i18n/en.js";
import { uk } from "./i18n/uk.js";

const TRANSLATIONS = { en, uk };

const LANG_KEY = "televault:lang";
const DEFAULT_LANG = "en";
const SUPPORTED_LANGS = ["en", "uk"];

function detectBrowserLang() {
  const nav = (navigator.language || DEFAULT_LANG).slice(0, 2).toLowerCase();
  return SUPPORTED_LANGS.includes(nav) ? nav : DEFAULT_LANG;
}

/** @returns {string} */
function getCurrentLang() {
  const stored = localStorage.getItem(LANG_KEY);
  return stored && SUPPORTED_LANGS.includes(stored)
    ? stored
    : detectBrowserLang();
}

/**
 * Translate a key using the current language, falling back to English, then to the raw key itself if no table has it.
 * A missing key is a content gap, not a crash — the raw key is visible enough to catch in review without breaking the page for the user.
 *
 * @param {string} key
 * @returns {string}
 */
function t(key) {
  const lang = getCurrentLang();
  const table = TRANSLATIONS[lang];
  const fallback = TRANSLATIONS[DEFAULT_LANG];
  return table?.[key] ?? fallback?.[key] ?? key;
}

/** Re-applies translations to every element with a data-i18n attribute. */
function applyTranslations() {
  document.documentElement.setAttribute("lang", getCurrentLang());
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
    el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria-label")));
  });
}

/**
 * Switches the active language, re-applies static (data-i18n) translations, and notifies the rest of the app.
 *
 * Why the event: applyTranslations() only reaches elements marked with data-i18n in the static HTML.
 * Views that build their own markup from fetched data (e.g. the Chats list rendering "{count} deleted" per row) aren't touched by it
 * — those views listen for this event and re-render their already-fetched data in the new language, without a network re-fetch.
 */
function setLang(lang) {
  if (!SUPPORTED_LANGS.includes(lang)) return;
  localStorage.setItem(LANG_KEY, lang);
  applyTranslations();
  document.dispatchEvent(new CustomEvent("televault:langchange"));
}

document.addEventListener("DOMContentLoaded", () => {
  applyTranslations();
  const select = document.getElementById("lang-select");
  if (select) {
    select.value = getCurrentLang();
    select.addEventListener("change", (e) => setLang(e.target.value));
  }
});

export { t, applyTranslations, setLang, getCurrentLang, SUPPORTED_LANGS };
