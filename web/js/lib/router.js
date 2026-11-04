/**
 * Hash routes of the chat view.
 *
 *     #/chats                       the Chats tab, no chat open
 *     #/chats/<chatId>              a chat, opened at its newest message
 *     #/chats/<chatId>/m/<id>       a chat, opened around one message (a search hit, a deleted message)
 *
 * Why the hash and not the History API (pushState): the app is served as static files with no server-side routing,
 * so a "real" path like /chats/42 would be a 404 on reload.
 * A fragment is never sent to the server, works from a bookmark, and fires `hashchange` for free on the Back/Forward buttons.
 *
 * Privacy: a route carries INTERNAL IDS ONLY - never message text, a search term or a name -
 * because URLs end up in browser history, bookmarks and screenshots, which message content must not.
 * <chatId> is Telegram's chat id (negative for groups and channels); <id> is the archive's own message id (`messages.id`), not Telegram's.
 *
 * Kept free of DOM access so it can be unit-tested on its own.
 */

// Telegram chat ids reach about -1.0e12 (supergroups/channels); the pattern is a loose shape check only -
// the API enforces the real bounds (utils/validation.py) and answers 422 for an out-of-range id.
const ROUTE_PATTERN = /^#\/chats(?:\/(-?\d{1,16})(?:\/m\/(\d{1,16}))?)?\/?$/;

/**
 * Read a chat-view route out of a location hash.
 *
 * @param {string} hash - `location.hash`, e.g. "#/chats/-100123/m/77".
 * @returns {{ chatId: number | null, messageId: number | null } | null}
 *   null when the hash is not a chat-view route at all (empty, or belongs to something else): callers leave the current screen alone in that case.
 *   `{ chatId: null }` is the bare "#/chats" route: the tab with no chat open.
 */
function parseChatRoute(hash) {
  const match = ROUTE_PATTERN.exec(hash);
  if (!match) return null;
  const chatId = match[1] === undefined ? null : Number(match[1]);
  const messageId = match[2] === undefined ? null : Number(match[2]);
  if (chatId !== null && !Number.isSafeInteger(chatId)) return null;
  if (messageId !== null && !Number.isSafeInteger(messageId)) return null;
  return { chatId, messageId };
}

/**
 * Build the hash for a chat (and optionally one message in it).
 * The inverse of parseChatRoute().
 *
 * @param {number | null} [chatId]
 * @param {number | null} [messageId]
 * @returns {string}
 */
function chatRouteHash(chatId = null, messageId = null) {
  if (chatId === null) return "#/chats";
  if (messageId === null) return `#/chats/${chatId}`;
  return `#/chats/${chatId}/m/${messageId}`;
}

export { parseChatRoute, chatRouteHash };
