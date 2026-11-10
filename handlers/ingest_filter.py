"""
The ingestion-time filters, as the live handlers use them: "must this event be ignored?" - because its chat is blocked (should_skip),
or because its text matches a message ignore rule (should_ignore_text).

What it is
    A per-account list of chats the owner chose NOT to archive (db/schema.py's ingest_blocked_chats, managed in Settings -> Blocked chats).
    Everything not on the list is archived, so a brand-new chat is always allowed until the owner blocks it.
    This is different from the display-time chat filter on the Messages/Deleted views, which only changes what is SHOWN from what is already archived.

Where it runs, and why there
    Right after the handler has its chat_id and BEFORE anything else: before the message text is resolved, before the Telegram entity lookups
    (event.get_chat()/get_sender() can cost real API calls, which a blocked and busy chat would otherwise keep spending), before any database write.
    A blocked chat's content must never be read, let alone stored - checking after the fact and deleting would still have written it once.
    Both handlers that can WRITE a chat's text go through here: handlers/on_message.py and handlers/on_edit.py
    (on_edit matters as much as on_message: an edit of an unknown message is inserted as a new row, and an edit of a known one stores the new text).
    backfill.py applies the same rule per chat (db.queries.is_chat_blocked() directly - it already holds a connection).

Why a database lookup per event and not an in-memory copy
    The live handler is low-volume (one real Telegram event at a time) and already does several database round-trips per archived message,
    so one indexed primary-key lookup is noise.
    In exchange a block takes effect on the very next message, with no refresh interval to explain,
    no stale copy that can disagree with what the Settings page shows, and no invalidation to get wrong.
    If an account ever receives enough traffic for this to show up in a profile, a short-lived cache is a small, local change to should_skip() - measure first.

When the rules cannot be read
    should_skip() answers True ("skip it") on any failure.
    A lost message is the normal cost of the database being down (the insert would have failed too),
    whereas guessing "not blocked" could archive a chat the owner asked us never to store.
    The one exception is an archive that predates the blocklist table:
    that provably has no rules, so db.queries.is_chat_blocked() itself answers False and archiving continues exactly as before (see its docstring).

What is deliberately NOT filtered: deletions (handlers/on_delete.py)
    A deletion event carries no new content - it only flags a row we already hold and snapshots text we already stored -
    and for private chats and legacy groups Telegram does not even say which chat it came from, so there is nothing to filter on.
    Blocking a chat stops FUTURE archiving; what was archived before stays, including the record that it was later deleted.
    Removing already-stored data is a separate, explicitly destructive action.

Message ignore rules (should_ignore_text)
    A second, finer filter that runs right AFTER the chat blocklist and before the message is resolved, looked up or stored:
    a message is ignored if its text CONTAINS a rule's phrase, ignoring case (matching: utils/text_matching.py; rules: db/schema.py's ingest_ignore_rules).
    A rule applies to every chat or only to the chats it lists; only rules for THIS chat are loaded.

    Only the text of a plain message is matched (matchable_text()): not a caption, not a file name, not a synthesized call label.
    A photo that carries a caption is therefore always archived, as before.
    A pasted link is still plain text: its preview is not a file.

    Edits are checked too (handlers/on_edit.py).
    An edit that makes the text match is dropped, so the stored version keeps its last text;
    an edit that makes an ignored message stop matching is stored like any edit of a message we never held (the message arrives as a new record).
    Messages already archived are never touched: a rule only affects what arrives after it exists, and backfill.py applies the same rules to history it fetches.

    Same failure policy as the blocklist: if the rules cannot be read the event is skipped
    (a lost message is the normal cost of the database being down, archiving a message the owner asked us never to store is not).
    An archive that predates the rule tables has no rules (db.queries.get_ignore_patterns()).

    PRIVACY: neither the phrase of a rule nor any message text is ever logged here - only chat and message ids.

Telegram ToS / privacy
    Blocking and ignoring only ever reduce what is stored.
    They do not change what TeleVault asks Telegram for, except that blocked chats no longer trigger entity lookups.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.exc import OperationalError
from telethon.tl.types import MessageMediaEmpty, MessageMediaWebPage

import db
from utils.text_matching import matches_any

logger = logging.getLogger(__name__)


def _lookup(chat_id: int) -> bool:
    """
    Synchronous database half of should_skip() - run via asyncio.to_thread() for the reason given in handlers/on_message.py's module docstring
    (a blocking network round-trip must not stall Telethon's event loop).
    Checks out its own pooled connection and returns it on exit, like every other worker-side database call.
    """
    with db.get_connection() as conn:
        return db.queries.is_chat_blocked(conn, chat_id)


async def should_skip(chat_id: int) -> bool:
    """
    True if an event from `chat_id` must be ignored: the chat is blocked, or the rules could not be read (see this module's docstring for why that also means skip).

    Never raises, so a handler can call it without its own error handling and still never crash the listener.
    Logs the chat id only - never any message content - because this runs before the content is even looked at.
    """
    try:
        return await asyncio.to_thread(_lookup, chat_id)
    except OperationalError:
        # Same short, readable line the handlers use for an unreachable database, instead of a full connection-pool traceback.
        logger.error(
            "Could not check the ingest blocklist for chat %s - database unreachable (Docker down?). Skipping the event.",
            chat_id,
        )
        return True
    except Exception:
        logger.exception(
            "Could not check the ingest blocklist for chat %s - skipping the event rather than risk archiving a blocked chat.",
            chat_id,
        )
        return True


def matchable_text(message: Any) -> str | None:
    """
    The text an ignore rule is compared with, or None if this message has none to compare.

    Telethon keeps a media message's caption in the same `message` field as the text of a plain message, and the archiver stores both as "text".
    The owner's rule is "the TEXT, not captions or file names", so a message is matchable only if it carries no file:
    `media` is empty, or just a link preview (a pasted URL is still a text message).
    Service messages and call labels (`action`) are never matched.
    Reads the raw `message` (not `text`, which re-adds markdown for bold/links), so formatting characters cannot break a phrase.
    """
    if getattr(message, "action", None) is not None:
        return None
    media = getattr(message, "media", None)
    if media is not None and not isinstance(media, (MessageMediaWebPage, MessageMediaEmpty)):
        return None
    return getattr(message, "message", None) or None


def _load_patterns(chat_id: int) -> list[str]:
    """
    Synchronous database half of should_ignore_text() - run via asyncio.to_thread() for the same reason as _lookup().
    Returns the folded phrases of every rule that applies to `chat_id`.
    """
    with db.get_connection() as conn:
        return db.queries.get_ignore_patterns(conn, chat_id)


async def should_ignore_text(chat_id: int, message: Any) -> bool:
    """
    True if `message` (an event's message, or one fetched by backfill) must be ignored because its text matches an ignore rule,
    or because the rules could not be read (see this module's docstring for why that also means ignore).

    A message with nothing to match (media, caption, service message, no text) answers False without touching the database.
    Never raises, so a handler can call it without its own error handling.
    Logs the chat id only - never a phrase, never text.
    """
    text = matchable_text(message)
    if text is None:
        return False
    try:
        patterns = await asyncio.to_thread(_load_patterns, chat_id)
    except OperationalError:
        logger.error(
            "Could not check the message ignore rules for chat %s - database unreachable (Docker down?). Skipping the event.",
            chat_id,
        )
        return True
    except Exception:
        logger.exception(
            "Could not check the message ignore rules for chat %s - skipping the event rather than risk archiving a message you asked to ignore.",
            chat_id,
        )
        return True
    return matches_any(patterns, text)
