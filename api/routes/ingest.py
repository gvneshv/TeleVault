"""
The ingestion-time chat blocklist - which chats TeleVault must NOT archive - for the CALLING account only:

    GET    /api/ingest/blocklist            - the account's blocked chats
    PUT    /api/ingest/blocklist/{chat_id}  - block a chat (idempotent)
    DELETE /api/ingest/blocklist/{chat_id}  - stop blocking a chat (idempotent)

How it works (the full policy is in handlers/ingest_filter.py, the table in db/schema.py's ingest_blocked_chats):
    The live archiver and backfill look a chat up in this list before they read or store anything from it, once per event / per chat.
    So a change made here takes effect on the next message, with no restart and nothing for this API to push to the workers.
    Everything not listed is archived - a new chat is always allowed until the owner blocks it here.

What blocking does NOT do:
    It stops FUTURE archiving only. Messages already archived stay, and show up in the views as before; removing them is a separate, destructive
    action that does not exist yet. Unblocking likewise recovers nothing - what arrived while the chat was blocked was never stored.

Only chats the archive already knows can be blocked (PUT answers 404 otherwise): the name shown in the list is copied from the archive's own record of the chat,
so the client never supplies free text, and there is nothing to validate beyond the id.

Writes go through api.dependencies.get_archive_write_connection (read-write); the GET uses the usual read-only get_archive_connection.
Both resolve the CALLER's own archive, so one account can never read or change another's blocklist.

Archives created before this feature have no table until `python scripts/migrate_archives.py` is run (RUNBOOK.md).
Every route here answers 409 "archive_outdated" for such an archive instead of a 500; the workers meanwhile simply archive everything, as they always did.
"""

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import db
from api.dependencies import get_archive_connection, get_archive_write_connection
from api.schemas import BlockedChatOut, BlockedChatRemovedOut, BlocklistOut
from db.read_queries import get_blocked_chats, get_chat
from utils.validation import INGEST_BLOCKLIST_MAX_COUNT, TELEGRAM_CHAT_ID_MAX, TELEGRAM_CHAT_ID_MIN

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Every route takes the chat id from the URL path; bounded so an absurd value is a 422 here instead of a numeric-overflow error from Postgres.
_ChatId = Path(ge=TELEGRAM_CHAT_ID_MIN, le=TELEGRAM_CHAT_ID_MAX, description="Telegram chat id, as shown in the archive.")


@contextmanager
def _needs_migrated_archive():
    """
    Turn "this archive has no ingest_blocked_chats table" into a clear 409 the UI can translate (reason "archive_outdated").
    Any OTHER database error is not ours to explain and propagates untouched.
    """
    try:
        yield
    except ProgrammingError as exc:
        if not db.is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database needs updating before chats can be blocked. Run `python scripts/migrate_archives.py` - see RUNBOOK.md.",
                "reason": "archive_outdated",
            },
        ) from exc


@router.get(
    "/blocklist",
    response_model=BlocklistOut,
    summary="List the chats that are not being archived",
)
def list_blocklist(conn: Connection = Depends(get_archive_connection)) -> dict[str, Any]:
    """Every chat on the caller's blocklist, newest block first, with the most that may be blocked (`max_count`)."""
    with _needs_migrated_archive():
        items = get_blocked_chats(conn)
    return {"items": items, "total": len(items), "max_count": INGEST_BLOCKLIST_MAX_COUNT}


@router.put(
    "/blocklist/{chat_id}",
    response_model=BlockedChatOut,
    summary="Stop archiving a chat",
)
def block_chat(chat_id: int = _ChatId, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """
    Block a chat: from the next message on it is neither archived nor edit-tracked. Already-archived messages are untouched.

    Idempotent - blocking a chat that is already blocked returns its existing rule. Otherwise:
        404 "chat_not_found" - the archive has never seen this chat (only chats it already knows can be blocked)
        409 "blocklist_full" - the account is at INGEST_BLOCKLIST_MAX_COUNT rules
    `chat_id` may be an old id of a group that has since become a supergroup; the rule is stored under the current one.
    """
    with _needs_migrated_archive():
        rules = get_blocked_chats(conn)
        canonical = db.queries.resolve_chat_id(conn, chat_id)

        existing = next((r for r in rules if r["chat_id"] == canonical), None)
        if existing is not None:
            return existing

        chat = get_chat(conn, canonical)
        if chat is None:
            raise HTTPException(
                status_code=404,
                detail={"message": f"Chat {chat_id} is not in the archive, so it cannot be blocked.", "reason": "chat_not_found"},
            )
        if len(rules) >= INGEST_BLOCKLIST_MAX_COUNT:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"At most {INGEST_BLOCKLIST_MAX_COUNT} chats can be blocked. Unblock one first.",
                    "reason": "blocklist_full",
                },
            )

        db.queries.block_chat(conn, canonical, chat["name"])
        return next(r for r in get_blocked_chats(conn) if r["chat_id"] == canonical)


@router.delete(
    "/blocklist/{chat_id}",
    response_model=BlockedChatRemovedOut,
    summary="Archive a chat again",
)
def unblock_chat(chat_id: int = _ChatId, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """
    Remove a chat from the blocklist: archiving resumes from the next message. Messages that arrived while it was blocked were never stored and cannot be recovered.
    Idempotent - `removed` is False if the chat was not blocked.
    """
    with _needs_migrated_archive():
        removed = db.queries.unblock_chat(conn, chat_id)
    return {"chat_id": chat_id, "removed": removed}
