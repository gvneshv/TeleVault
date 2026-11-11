"""
Keeps a copy of how far the account owner has READ each chat in Telegram, so the web UI can draw an "Unread messages" bar where Telegram would.

Two sources feed it, because neither is enough alone:
  - sync_read_state(): the chat list (`iter_dialogs`) at start-up.
    Whatever was read while the archiver was not running is picked up here.
  - the live MessageRead(inbox=True) event: the owner reads a chat on another device, and Telegram tells every other session of the account.
    (The archiver is one of them. Telegram does not report a read to the session that did the reading, which is not us - we never read anything.)

What is stored: one number per chat, chats.read_inbox_max_id - Telegram's own `read_inbox_max_id`: every message with an id up to it has been read.
The chat view compares it with the ids of the archived messages.
Nothing else about reading is kept (no times, no history of positions).

THIS MODULE NEVER MARKS ANYTHING AS READ.
It only listens and stores what Telegram reports; no read acknowledgement, no "mark as read" call, nothing is sent.
(Marking messages read on the owner's behalf is a different, deliberately separate roadmap item - see CHANGELOG "Auto-read policies".)
iter_dialogs() and the event handlers are passive in Telethon: fetching a chat list does not read any chat.

Pitfalls this module exists to avoid
  - events.MessageRead(inbox=True) also fires when the owner PLAYS A VOICE NOTE (`event.contents` is True),
    and then `max_id` is that one message's id, not a reading position.
    Treating it as one would mark everything up to that message as read.
    Those events are ignored.
  - Outbox events (someone else read the owner's messages) are the opposite direction and a different feature (Phase 4, "read receipt inference");
    inbox=True keeps them out, and a test pins that.
  - A chat on the ingest blocklist is not archived, and neither is the owner's reading activity in it: blocked chats are skipped.
  - A position is stored only for chats the archive already holds, and only moves forward - see db.queries.record_read_inboxes().

Failure policy: this is decoration.
Any error is logged (ids and counts only, never message text) and swallowed; it must never get in the way of archiving.
An archive that has not been migrated gets one warning from db.queries and is otherwise left alone.

Telegram ToS / privacy: reading the position uses the same session and the same updates the archiver already receives;
it asks Telegram for nothing new except the chat list at start-up, which backfill.py also requests.
The stored number says how far the owner has read - the owner's own data, kept in their own archive, returned only to them and never logged.

Threading: the database calls are blocking, so they run through asyncio.to_thread() (see on_message.py for the reasoning).
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.exc import OperationalError
from telethon import events

import db
from handlers import ingest_filter

logger = logging.getLogger(__name__)


def _valid_position(value: Any) -> int | None:
    """`value` as a usable read position, or None.
    Telegram's ids are positive integers; 0 means "nothing read yet" and is valid, negatives and non-integers are not."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _persist_position(chat_id: int, max_id: int) -> None:
    """Synchronous DB work for one live event (run via asyncio.to_thread)."""
    with db.get_connection() as conn:
        db.queries.record_read_inbox(conn, chat_id, max_id)


def _persist_positions(positions: list[tuple[int, int]]) -> int:
    """
    Synchronous DB work for the start-up sync: store the positions of every chat that is not blocked.
    Returns how many were handed to the database.
    The blocklist is read here, on the same connection, so the whole sync costs one connection instead of one per dialog.
    If the blocklist cannot be read the whole sync is abandoned (the caller logs it): a blocked chat's reading activity must not be stored on a guess.
    """
    with db.get_connection() as conn:
        allowed = [(chat_id, max_id) for chat_id, max_id in positions if not db.queries.is_chat_blocked(conn, chat_id)]
        db.queries.record_read_inboxes(conn, allowed)
    return len(allowed)


async def sync_read_state(client) -> None:
    """
    Copy the read position of every chat from Telegram's chat list into the archive.
    Run once, in the background, after the handlers are registered.

    Best effort and never raises (except CancelledError, so shutdown still works): see the module docstring.
    """
    try:
        positions: list[tuple[int, int]] = []
        async for dialog in client.iter_dialogs():
            # The custom Dialog object does not carry the read position; the raw TL dialog it wraps does.
            position = _valid_position(getattr(getattr(dialog, "dialog", None), "read_inbox_max_id", None))
            chat_id = getattr(dialog, "id", None)
            if position is not None and isinstance(chat_id, int):
                positions.append((chat_id, position))
        stored = await asyncio.to_thread(_persist_positions, positions)
        logger.info("Read positions synced from Telegram's chat list (%d of %d chats).", stored, len(positions))
    except asyncio.CancelledError:
        raise
    except OperationalError:
        logger.error("Could not store the chats' read positions - database unreachable (Docker down?). The 'Unread messages' bar may be out of date.")
    except Exception:
        logger.exception("Could not sync the chats' read positions from Telegram. The 'Unread messages' bar may be out of date until the next restart.")


def register(client) -> None:
    """Attach the live read-position handler to the given Telethon client."""

    @client.on(events.MessageRead(inbox=True))
    async def on_inbox_read(event: events.MessageRead.Event) -> None:
        """The owner read a chat somewhere else: remember how far.
        See the module docstring for what is ignored and why."""
        if event.contents:
            return  # a voice note was played, not a reading position (see the module docstring)
        chat_id = event.chat_id
        max_id = _valid_position(event.max_id)
        if chat_id is None or max_id is None:
            return

        if await ingest_filter.should_skip(chat_id):
            logger.debug("Not recording the read position of chat %s - it is blocked (or the blocklist could not be read).", chat_id)
            return

        try:
            await asyncio.to_thread(_persist_position, chat_id, max_id)
        except OperationalError:
            logger.error("Could not store the read position of chat %s - database unreachable (Docker down?).", chat_id)
        except Exception:
            logger.exception("Could not store the read position of chat %s.", chat_id)
