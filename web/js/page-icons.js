/**
 * Draws the SVG icons (the `data-icon="..."` placeholders) on the pages that are NOT the app shell:
 * login, register, Telegram setup, the legal pages and the 404 page.
 *
 * Why a separate file: the app shell gets its icons from app.js, but these pages load only their own small script (or none),
 * so their theme button kept the text glyph ("sun") that the shell had already replaced with the real icons.
 * theme.js leaves a button alone once it contains a `[data-icon]` element,
 * and the CSS keyed on <html data-theme> (css/base.css, ".theme-toggle__icon") shows the right one, so all this page needs is the drawing step.
 *
 * A module is deferred, so the markup is already parsed when this runs; no DOMContentLoaded listener is needed.
 */
import { renderIcons } from "./lib/icons.js";

renderIcons();
