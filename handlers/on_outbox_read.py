"""
Keeps a copy of how far the OTHER person has read the owner's messages in a PRIVATE chat - Telegram's "double check mark" - so the web UI can draw it.

The counterpart of handlers/on_read.py, which mirrors the opposite direction (how far the OWNER has read).
Two sources feed it: this live event, and the start-up sync in on_read.sync_read_state() (which already walks the chat list once and reads both numbers).

What is stored: one number per private chat, chats.read_outbox_max_id - every message of the owner's with an id up to it has been read by the other person.
Nothing else is kept: no times, no history of positions.

PRIVATE CHATS ONLY, on purpose (owner's decision): in a group the same feature means following many people's reading, which brings flood limits and legal exposure.
Three independent guards keep it that way:
the handler returns early for anything that is not a private chat, the database layer refuses to store anything but a private chat (db.queries.record_read_outboxes),
and the API reports the position only for private chats.

THIS MODULE NEVER SENDS ANYTHING.
It listens to the updates Telegram already delivers to this session and stores a number;
no read acknowledgement, no request of its own, so nobody learns that the archive exists or what it reads.

What Telegram decides, not us: if the other person has turned read receipts off, Telegram simply never reports their reading,
and the chat view shows the owner's messages as "not read" - which cannot be told apart from "not read yet".
(That is how Telegram's own apps behave too; the exact rules are Telegram's and have changed before, so treat the marks as a hint.)

Failure policy: this is decoration.
Any error is logged (chat ids only, never message text) and swallowed; it must never get in the way of archiving.
An archive that has not been migrated gets one warning from db.queries and is otherwise left alone.

Threading: the database calls are blocking, so they run through asyncio.to_thread() (see on_message.py for the reasoning).
"""

import asyncio
import logging

from sqlalchemy.exc import OperationalError
from telethon import events

import db
from handlers import ingest_filter
from handlers.on_read import _valid_position

logger = logging.getLogger(__name__)


def _persist_position(chat_id: int, max_id: int) -> None:
    """Synchronous DB work for one live event (run via asyncio.to_thread)."""
    with db.get_connection() as conn:
        db.queries.record_read_outbox(conn, chat_id, max_id)


def register(client) -> None:
    """Attach the live handler for "somebody read the owner's messages" to the given Telethon client."""

    @client.on(events.MessageRead(inbox=False))
    async def on_outbox_read(event: events.MessageRead.Event) -> None:
        """The other person read the owner's messages: remember how far.
        Private chats only - see the module docstring."""
        if not event.is_private:
            return
        chat_id = event.chat_id
        max_id = _valid_position(event.max_id)
        if chat_id is None or max_id is None:
            return

        if await ingest_filter.should_skip(chat_id):
            logger.debug("Not recording the read receipt of chat %s - it is blocked (or the blocklist could not be read).", chat_id)
            return

        try:
            await asyncio.to_thread(_persist_position, chat_id, max_id)
        except OperationalError:
            logger.error("Could not store the read receipt of chat %s - database unreachable (Docker down?).", chat_id)
        except Exception:
            logger.exception("Could not store the read receipt of chat %s.", chat_id)
