"""
Handles Telethon's NewMessage event — fired for every message that arriveson the account, both incoming and outgoing (including Saved Messages).

Flow per event:
  1. Resolve the text to store — either the message body or a synthesized call label for MessageActionPhoneCall service messages.
  2. Skip if there is nothing worth archiving (media-only, stickers, etc.).
  3. Extract the chat entity and upsert it into `chats`.
  4. Extract the sender entity (if any) and upsert it into `senders`.
  5. Insert the message into `messages`.

We call `await event.get_chat()` and `await event.get_sender()` rather than reading `event.chat` / `event.sender` directly.
The direct attributes are only populated when Telegram includes the full entity in the update packet, which isn't guaranteed — the async getters always fetch from cache or the server.

Threading note (Postgres migration):
    Steps 3-5 above (chat/sender upserts, message insert) are plain, blocking calls - each one a real network round-trip to Postgres.
    Under SQLite this ran inline on the event loop with no real cost (local file I/O).
    Under Postgres it would block Telethon's entire event loop for the duration of each round-trip - including its own network I/O - for every single message.
    The DB work below is wrapped in asyncio.to_thread() so it runs in a worker thread instead, leaving the event loop free.
    The migration-detection branches further down get the same treatment, for the same reason.
"""

import asyncio
import logging

from telethon import events, utils
from telethon.tl.types import (
    MessageActionChatMigrateTo,
    MessageActionChannelMigrateFrom,
    PeerChat,
    PeerChannel,
)
from sqlalchemy.exc import OperationalError

import db
from .helpers import get_chat_type, get_sender_fields, resolve_message_text

logger = logging.getLogger(__name__)


def _record_migration(old_chat_id: int, new_chat_id: int) -> None:
    """
    Synchronous DB work for a detected chat migration - see this module's docstring for why this runs via asyncio.to_thread() rather than inline.

    Checks out and returns its own pooled connection (db/connection.py's "checkout, use, close" contract) - a fresh one per call,
    matching how infrequently migrations actually fire (once per chat, ever) rather than holding a connection open across the whole handler's lifetime.
    """
    with db.get_connection() as conn:
        db.queries.record_chat_migration(
            conn, old_chat_id=old_chat_id, new_chat_id=new_chat_id
        )


def _persist_message(
    chat_id: int,
    chat_name: str | None,
    chat_type: str,
    chat_username: str | None,
    sender_id: int | None,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    tg_message_id: int,
    text: str,
    date,
) -> None:
    """
    Synchronous DB work for a single archived message - see this module's docstring for why this runs via asyncio.to_thread() rather than inline.

    Takes plain values rather than the Telethon event/message objects themselves - those objects
    (and the async get_chat()/get_sender() calls that resolve them) belong to the event loop;
    only their already-resolved field values are handed off to this worker-thread function.
    """
    with db.get_connection() as conn:
        # Upsert chat and sender before the message insert — both are FKs.
        db.queries.upsert_chat(
            conn,
            chat_id=chat_id,
            name=chat_name,
            chat_type=chat_type,
            username=chat_username,
            commit=False,
        )

        if sender_id is not None:
            db.queries.upsert_sender(
                conn,
                sender_id=sender_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                commit=False,
            )

        db.queries.insert_message(
            conn,
            tg_message_id=tg_message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            date=date,
        )


def register(client) -> None:
    """Attach the NewMessage handler to the given Telethon client."""

    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event) -> None:
        """
        Persist an incoming or outgoing message to the database.

        Regular text messages are stored as-is.
        Call service messages (MessageActionPhoneCall) are stored with a synthesized label such as "[Missed call]" or "[Voice call · 2 min 17 sec]" so they appear meaningfully in the archive and can be flagged as deleted like any other row.

        All other service messages and media-only messages (stickers, photos, etc.) are skipped — out of scope for Phase 1.
        """
        message = event.message

        # --- Chat migration detection ---------------------------------------
        # Fires once in each chat at the moment a basic group is upgraded to a supergroup.
        # Neither message carries archivable text, so this must be handled before resolve_message_text()/the "not text" skip below.
        action = getattr(message, "action", None)
        if isinstance(action, MessageActionChatMigrateTo):
            # Fires in the OLD chat. event.chat_id is the old id; action.channel_id
            # is the new supergroup's raw (unmarked) id.
            new_chat_id = utils.get_peer_id(PeerChannel(action.channel_id))
            try:
                await asyncio.to_thread(_record_migration, event.chat_id, new_chat_id)
            except OperationalError:
                logger.error(
                    "Could not record chat migration %s -> %s - database unreachable (Docker down?).",
                    event.chat_id,
                    new_chat_id,
                )
            except Exception:
                logger.exception(
                    "Failed to record chat migration %s -> %s.",
                    event.chat_id,
                    new_chat_id,
                )
            return
        if isinstance(action, MessageActionChannelMigrateFrom):
            # Fires in the NEW supergroup. event.chat_id is the new id; action.chat_id
            # is the old basic group's raw (unmarked) id.
            old_chat_id = utils.get_peer_id(PeerChat(action.chat_id))
            try:
                await asyncio.to_thread(_record_migration, old_chat_id, event.chat_id)
            except OperationalError:
                logger.error(
                    "Could not record chat migration %s -> %s - database unreachable (Docker down?).",
                    old_chat_id,
                    event.chat_id,
                )
            except Exception:
                logger.exception(
                    "Failed to record chat migration %s -> %s.",
                    old_chat_id,
                    event.chat_id,
                )
            return

        # --- Resolve the text to archive -----------------------------------
        # See handlers/helpers.py's resolve_message_text() - shared with backfill.py so both apply the same archiving rules.
        text = resolve_message_text(message)

        # --- Pre-flight guards ---------------------------------------------

        # Telethon can return None for chat_id on certain service messages or protocol edge cases.
        # Without a valid chat_id we can't satisfy the FK constraint in messages, so skip rather than error.
        if event.chat_id is None:
            logger.warning(
                "Skipping message %s with no chat_id in event %s.",
                message.id,
                event,
            )
            return

        # Skip anything we have no text for — media, stickers, unsupported service messages, etc.
        # Call labels produced above are truthy, so they pass this guard correctly.
        if not text:
            logger.debug(
                "Skipping non-text message %s in chat %s.",
                message.id,
                event.chat_id,
            )
            return

        # --- Persist -------------------------------------------------------
        try:
            chat = await event.get_chat()
            sender = await event.get_sender()

            chat_type = get_chat_type(chat)
            chat_name = getattr(chat, "title", None) or getattr(
                chat, "first_name", None
            )
            # Strip leading '@' if Telegram includes it (it usually doesn't, but be safe).
            chat_username = (getattr(chat, "username", None) or "").lstrip("@") or None
            username, first_name, last_name = get_sender_fields(sender)

            await asyncio.to_thread(
                _persist_message,
                event.chat_id,
                chat_name,
                chat_type,
                chat_username,
                event.sender_id,
                username,
                first_name,
                last_name,
                message.id,
                text,
                message.date,
            )

        except OperationalError:
            # Specifically "database unreachable" (Docker down, network blip, etc.) - a short, readable line rather than a full connection-pool traceback.
            # Anything else still falls to the except Exception below and gets logger.exception().
            logger.error(
                "Could not archive message %s in chat %s - database unreachable (Docker down?).",
                message.id,
                event.chat_id,
            )
        except Exception:
            # Log and swallow — a single failed insert should never crash the listener.
            # The message will simply be absent from the archive.
            logger.exception(
                "Failed to archive message %s in chat %s.",
                message.id,
                event.chat_id,
            )