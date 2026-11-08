"""
Who a sender is, and what the owner calls them - for the CALLING account's archive only:

    GET    /api/senders/display-names        - every label the owner has given, to apply in the UI in one request
    GET    /api/senders/{id}                 - one sender with their label and the names TeleVault has seen them under
    PUT    /api/senders/{id}/display-name    - set (or change) the label
    DELETE /api/senders/{id}/display-name    - remove the label (idempotent)

A label is a display preference of the owner and nothing else: it is never sent to Telegram, never changes what was archived,
and is applied by the web UI, not by the message endpoints (they keep returning `display_name: null`; see db/schema.py's sender_display_names).
Because the UI fetches all labels once and applies them itself, a rename does not touch pagination, search or the 10 s live refresh of the chat view.

The name history (`history` in GET /senders/{id}) is filled by the archiver as it sees names change (db/queries.py's upsert_sender()).
Only changes seen from the migration onwards exist; nothing is reconstructed for the past.
See sender_name_history in db/schema.py for what "seen" means.

Privacy: everything here is data about people other than the account owner, but it is data the archive already holds
(the names Telegram itself showed the account) - nothing new is collected, and each account only ever reaches its own archive.

Writes go through api.dependencies.get_archive_write_connection (read-write); the two GETs use the usual read-only get_archive_connection.
Archives created before this feature have no tables until `python scripts/migrate_archives.py` is run (RUNBOOK.md).
Reads stay quiet on such an archive (no labels, a history of one entry); writes answer 409 "archive_outdated" instead of a 500.
"""

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import db
from api.dependencies import get_archive_connection, get_archive_write_connection
from api.schemas import DisplayNameIn, DisplayNameOut, DisplayNameRemovedOut, DisplayNamesOut, SenderDetailOut
from db.read_queries import get_sender_detail, get_sender_display_names, sender_exists
from utils.validation import DISPLAY_NAME_MAX_COUNT, TELEGRAM_SENDER_ID_MAX, TELEGRAM_SENDER_ID_MIN

router = APIRouter(prefix="/senders", tags=["senders"])

# Taken from the URL path; bounded so an absurd value is a 422 here instead of a numeric-overflow error from Postgres.
_SenderId = Path(ge=TELEGRAM_SENDER_ID_MIN, le=TELEGRAM_SENDER_ID_MAX, description="Telegram user id, as shown in the archive.")


@contextmanager
def _needs_migrated_archive():
    """
    Turn "this archive has no sender_display_names table" into a clear 409 the UI can translate (reason "archive_outdated").
    Any OTHER database error is not ours to explain and propagates untouched.
    (Same idea as api/routes/ingest.py's helper of the same name; the message differs because the feature does.)
    """
    try:
        yield
    except ProgrammingError as exc:
        if not db.is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database needs updating before display names can be saved. Run `python scripts/migrate_archives.py` - see RUNBOOK.md.",
                "reason": "archive_outdated",
            },
        ) from exc


def _sender_not_found(sender_id: int) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message": f"Sender {sender_id} is not in the archive.", "reason": "sender_not_found"},
    )


# Declared BEFORE "/{sender_id}" on purpose: the two paths have the same shape, and FastAPI takes the first route that matches.
# ("display-names" is not an int, so the other order would answer 422 instead of reaching this route.)
@router.get(
    "/display-names",
    response_model=DisplayNamesOut,
    summary="List every display name the owner has set",
)
def list_display_names(conn: Connection = Depends(get_archive_connection)) -> dict[str, Any]:
    """All labels of the caller's archive, ordered by sender id, plus the most that may exist (`max_count`). Empty on an archive that has not been migrated."""
    items = get_sender_display_names(conn)
    return {"items": items, "total": len(items), "max_count": DISPLAY_NAME_MAX_COUNT}


@router.get(
    "/{sender_id}",
    response_model=SenderDetailOut,
    summary="One sender with their display name and name history",
)
def get_sender(sender_id: int = _SenderId, conn: Connection = Depends(get_archive_connection)) -> dict[str, Any]:
    """404 "sender_not_found" if the archive has never seen this id."""
    detail = get_sender_detail(conn, sender_id)
    if detail is None:
        raise _sender_not_found(sender_id)
    return detail


@router.put(
    "/{sender_id}/display-name",
    response_model=DisplayNameOut,
    summary="Set a sender's display name",
)
def set_display_name(
    body: DisplayNameIn,
    sender_id: int = _SenderId,
    conn: Connection = Depends(get_archive_write_connection),
) -> dict[str, Any]:
    """
    Give a sender the owner's own label; changing an existing label is the same call.
    The text is trimmed and bounded (api/schemas/sender.py).

        404 "sender_not_found"  - the archive has never seen this id (only known senders can be labelled)
        409 "display_names_full" - a NEW label would exceed DISPLAY_NAME_MAX_COUNT (changing an existing one never does)
        409 "archive_outdated"  - the archive has not been migrated yet
    """
    with _needs_migrated_archive():
        if not sender_exists(conn, sender_id):
            raise _sender_not_found(sender_id)
        existing = get_sender_display_names(conn)
        if len(existing) >= DISPLAY_NAME_MAX_COUNT and all(item["sender_id"] != sender_id for item in existing):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"At most {DISPLAY_NAME_MAX_COUNT} senders can have a display name. Remove one first.",
                    "reason": "display_names_full",
                },
            )
        return db.queries.set_sender_display_name(conn, sender_id, body.display_name)


@router.delete(
    "/{sender_id}/display-name",
    response_model=DisplayNameRemovedOut,
    summary="Remove a sender's display name",
)
def remove_display_name(sender_id: int = _SenderId, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """The sender goes back to the name Telegram gave them.
    Idempotent - `removed` is False if there was no label."""
    with _needs_migrated_archive():
        removed = db.queries.clear_sender_display_name(conn, sender_id)
    return {"sender_id": sender_id, "removed": removed}
