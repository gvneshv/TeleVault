"""
The ingestion-time blocklist, as the live handlers use it: "must this event be ignored because its chat is blocked?"

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

Telegram ToS / privacy
    Blocking only ever reduces what is stored.
    It does not change what TeleVault asks Telegram for, except that blocked chats no longer trigger entity lookups.
"""

import asyncio
import logging

from sqlalchemy.exc import OperationalError

import db

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
