/**
 * Small helper shared by the views that re-fetch quietly when their tab is switched back to
 * (chats, messages, deleted, stats, health - see the refresh*View() exports and app.js's showView()).
 *
 * Why a "silent" refresh needs its own rule: the normal load replaces the view with a "Loading…" placeholder and then redraws everything,
 * which on a tab switch would flash, throw away the scroll position and collapse anything the person had expanded (the Deleted tab's "View details").
 * So a silent refresh keeps the current content on screen and only redraws when the server's answer is actually different from what is shown.
 */

/**
 * True when `data` is exactly what `lastRender` already shows, i.e. redrawing would change nothing.
 *
 * Compared as JSON text: the API answers with plain JSON, the same query returns keys in the same order, and a false "changed" only costs a redraw,
 * so there is no need for a deep-equal dependency.
 *
 * @param {{ type: string, data?: unknown } | null | undefined} lastRender - the view's last rendered state (see chatsViewState.lastRender in chats.js).
 * @param {unknown} data - the freshly fetched response body.
 * @returns {boolean}
 */
function isUnchanged(lastRender, data) {
  return (
    lastRender?.type === "data" &&
    JSON.stringify(lastRender.data) === JSON.stringify(data)
  );
}

export { isUnchanged };
