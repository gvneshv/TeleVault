/**
 * The owner's own names for senders (GET /api/senders/display-names), shared by the chat view and Settings.
 *
 * A label belongs to a SENDER.
 * In a private chat the chat id IS the other person's user id (positive; groups and channels have negative ids),
 * so the same label also names that private chat in the chat list, the chat header and the Settings pickers -
 * which is what tells apart friends who have the same names.
 * A group is never relabelled: its name belongs to the group, and its members are labelled above their messages.
 */

import { apiFetch } from "./auth.js";

/**
 * Fetch every label as a Map(sender id -> label).
 * Never throws: an archive that has not been migrated answers an empty list, and on any failure Telegram's own names are simply shown.
 * @returns {Promise<Map<number, string>>}
 */
export async function fetchDisplayNames() {
  try {
    const res = await apiFetch("/api/senders/display-names");
    if (!res.ok) return new Map();
    const data = await res.json();
    return new Map(
      data.items.map((item) => [item.sender_id, item.display_name]),
    );
  } catch {
    return new Map();
  }
}

/** @param {{ chat_id?: number, chat_type?: string } | null | undefined} chat @returns {boolean} true for a one-to-one chat (the picker lists have no chat_type, so the id's sign decides there). */
export function isPrivateChat(chat) {
  if (!chat || typeof chat.chat_id !== "number") return false;
  return chat.chat_type ? chat.chat_type === "private" : chat.chat_id > 0;
}

/**
 * The name to show for a chat: the owner's label for a private chat that has one, otherwise `fallback` (the name Telegram gave it).
 * @param {Map<number, string>} labels @param {object | null | undefined} chat @param {string} fallback @returns {string}
 */
export function chatDisplayName(labels, chat, fallback) {
  return (isPrivateChat(chat) && labels.get(chat.chat_id)) || fallback;
}
