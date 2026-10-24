"""
Handles Telethon's MessageEdited event - fired when a message's text is changed by its sender.

Why we bother archiving edits:
  A common pattern is editing -> deleting.
  Someone writes something, edits it (maybe to soften it), then deletes it.
  Without the edit history, the archive shows only the final text before deletion. With it, you get the full picture.

Flow per event:
  0. Skip the event if its chat is on the ingest blocklist, before anything else - see handlers/ingest_filter.py.
     An edit is NOT harmless to ignore the filter for: it stores the new text of a known message, and for an unknown one it inserts the whole message,
     so a blocked chat would otherwise keep leaking its content through edits.
  1. Confirm the edited message has text (skip media caption changes for now).
  2. Call queries.record_edit, which:
       a. Fetches the current text from the DB (becomes old_text).
       b. Writes a row to message_edits with old -> new.
       c. Updates messages with the new text and sets is_edited = TRUE.
  3. Ensure the chat and sender are in the DB first, same as on_message.
     (Edge case: a message could be edited in a chat we somehow missed archiving - the FK would fail without the upsert guard.)

Threading note (Postgres migration):
    Step 2/3's DB calls are blocking network round-trips to Postgres now rather than local SQLite file I/O,
    so they run via asyncio.to_thread() instead of inline - see on_message.py's module docstring for the full reasoning, which applies identically here.
"""

import asyncio
import logging

from telethon import events
from sqlalchemy.exc import OperationalError

import db
from handlers import ingest_filter
from handlers.helpers import get_chat_type, get_sender_fields

logger = logging.getLogger(__name__)


def _persist_edit(
    chat_id: int,
    chat_name: str | None,
    chat_type: str,
    chat_username: str | None,
    sender_id: int | None,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    tg_message_id: int,
    new_text: str,
    edited_at,
    date,
) -> None:
    """
    Synchronous DB work for a single edit event - see this module's docstring for why this runs via asyncio.to_thread() rather than inline.
    """
    with db.get_connection() as conn:
        # Guard: ensure the chat and sender rows exist.
        # If this edit arrives for a message we never archived (e.g. TeleVault was offline when it was sent), the upserts create the parent rows so the FK constraints don't blow up.
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

        found = db.queries.record_edit(
            conn,
            tg_message_id=tg_message_id,
            chat_id=chat_id,
            new_text=new_text,
            edited_at=edited_at,
        )

        if not found:
            # The original message isn't in our DB - insert it now with the current (post-edit) text.
            # Not ideal, but better than having no record of this message at all.
            logger.info(
                f"Edit received for unknown message {tg_message_id} in chat {chat_id} - inserting current version as a new record."
            )
            db.queries.insert_message(
                conn,
                tg_message_id=tg_message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                text=new_text,
                date=date,
                is_edited=True,
            )


def register(client) -> None:
    """
    Attach the MessageEdited handler to the given Telethon client.
    """

    @client.on(events.MessageEdited)
    async def on_message_edited(event: events.MessageEdited.Event) -> None:
        """
        Record a message edit: snapshot old text, store new text, update the live row in `messages`.
        """
        message = event.message

        # Telethon types event.chat_id as Optional (it can be None for odd service updates).
        # The DB layer needs a real id for its FK upserts, so bail out early - same guard on_message.py applies.
        # Binding it to a local also narrows the type for the whole handler.
        chat_id = event.chat_id
        if chat_id is None:
            logger.warning("Skipping edit event for message %s - no chat_id in event.", message.id)
            return

        # Before every other check (and so before any content or entity is touched) - see this module's docstring, step 0.
        if await ingest_filter.should_skip(chat_id):
            logger.debug(
                "Skipping edit of message %s in chat %s - chat is blocked (or the blocklist could not be read).",
                message.id,
                chat_id,
            )
            return

        if event.sender_id is None:
            logger.warning(
                f"Edit event for message {message.id} in chat {chat_id} - no sender ID in event."
            )
            return

        # Same rationale as on_message - skip non-text edits for now.
        if not message.text:
            logger.debug(
                f"Skipping non-text edit for message {message.id} in chat {chat_id}."
            )
            return

        try:
            chat = await event.get_chat()
            sender = await event.get_sender()

            chat_type = get_chat_type(chat)
            chat_name = getattr(chat, "title", None) or getattr(
                chat, "first_name", None
            )
            chat_username = (getattr(chat, "username", None) or "").lstrip("@") or None
            username, first_name, last_name = get_sender_fields(sender)

            # edit_date is set by Telegram when the message is edited.
            # Fall back to None - queries.record_edit uses _now() in that case.
            edit_date = getattr(message, "edit_date", None)

            await asyncio.to_thread(
                _persist_edit,
                chat_id,
                chat_name,
                chat_type,
                chat_username,
                event.sender_id,
                username,
                first_name,
                last_name,
                message.id,
                message.text,
                edit_date,
                message.date,
            )
        except OperationalError:
            logger.error(
                "Could not record edit for message %s in chat %s - database unreachable (Docker down?).",
                message.id,
                chat_id,
            )
        except Exception:
            logger.exception(
                f"Failed to record edit for message {message.id} in chat {chat_id}."
            )