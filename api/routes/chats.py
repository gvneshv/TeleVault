"""
Chat-related endpoints:

    GET /api/chats                      - paginated list of all known chats
    GET /api/chats/options              - every chat's {chat_id, name}, unpaginated (for filter dropdowns)
    GET /api/chats/{chat_id}            - single chat with aggregate counts
    GET /api/chats/{chat_id}/messages   - paginated messages within one chat
    GET /api/chats/{chat_id}/window     - an anchored, cursor-based window of one chat's conversation (the chat view)
    DELETE /api/chats/{chat_id}         - remove everything stored for one chat (irreversible; the one write route in this module)
"""

from sqlalchemy.engine import Connection
from datetime import datetime, timezone
from math import ceil
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.dependencies import get_archive_connection, get_archive_write_connection, get_current_user
from api.process_utils import is_backfill_running
from api.schemas import ChatOut, ChatOption, ChatPurgedOut, ChatWindowOut, MessageOut, PaginatedResponse
from utils.validation import (
    CHAT_WINDOW_DEFAULT,
    CHAT_WINDOW_MAX,
    DATE_PARAM_MAX_LENGTH,
    MESSAGE_ID_MAX,
    MESSAGE_ID_MIN,
    SEARCH_QUERY_MAX_LENGTH,
    TELEGRAM_CHAT_ID_MAX,
    TELEGRAM_CHAT_ID_MIN,
)
from db.queries import purge_chat as purge_chat_data  # not `import db`: the older routes here name their connection parameter `db`
from db.read_queries import get_chat, get_chats, get_chat_options, get_chat_messages, get_chat_window
from utils.security import DecodedAccessToken
from utils.worker_paths import backfill_status_path

router = APIRouter(tags=["chats"])

# The chat id as it appears in the URL of the chat-view routes; bounded so an absurd value is a 422, not a numeric-overflow error from Postgres.
_ChatId = Path(ge=TELEGRAM_CHAT_ID_MIN, le=TELEGRAM_CHAT_ID_MAX, description="Telegram chat id, as shown in the archive.")


def _build_page(result: dict[str, Any], page: int, per_page: int) -> dict[str, Any]:
    """Attach pagination metadata to a raw _paginate() result."""
    total = result["total"]
    return {
        "items": result["items"],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, ceil(total / per_page)),
    }


@router.get(
    "/chats",
    response_model=PaginatedResponse[ChatOut],
    summary="List all archived chats",
)
def list_chats(
    page: int = Query(1, ge=1, description="Page number (1-based)."),
    per_page: int = Query(50, ge=1, le=200, description="Results per page."),
    order: Literal["asc", "desc"] = Query(
        "desc",
        description="Sort by most recent activity descending (default) or ascending (least recently active first).",
    ),
    db: Connection = Depends(get_archive_connection),
) -> dict[str, Any]:
    """
    Return all chats TeleVault has seen, sorted by most recent message (same ordering as the Telegram sidebar) by default.

    Each chat includes aggregate counts (total messages, deleted messages) and a preview of the most recent message text, so the sidebar can be rendered without additional per-chat requests.
    """
    result = get_chats(db, page=page, per_page=per_page, order=order)
    return _build_page(result, page, per_page)


@router.get(
    "/chats/options",
    response_model=list[ChatOption],
    summary="List every chat's id and name, unpaginated",
)
def list_chat_options(db: Connection = Depends(get_archive_connection)) -> list[dict[str, Any]]:
    """
    Return {chat_id, name} for every chat TeleVault has seen, with no pagination.

    Powers the chat-filter dropdown on the Messages/Deleted views,
    which needs the full list to build its checkbox options - unlike GET /api/chats, this is not meant for a paginated sidebar.
    Registered ahead of /chats/{chat_id} below so "options" is matched as this literal path, not swallowed as an attempted (and invalid) {chat_id} value.
    """
    return get_chat_options(db)


@router.get(
    "/chats/{chat_id}",
    response_model=ChatOut,
    summary="Get a single chat by ID",
)
def get_chat_by_id(
    chat_id: int,
    db: Connection = Depends(get_archive_connection),
) -> dict[str, Any]:
    """
    Return a single chat record with aggregate counts.
    Raises 404 if the chat_id is not in the archive.
    """
    row = get_chat(db, chat_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found.")
    return row


@router.get(
    "/chats/{chat_id}/messages",
    response_model=PaginatedResponse[MessageOut],
    summary="List messages in a chat",
)
def list_chat_messages(
    chat_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, max_length=SEARCH_QUERY_MAX_LENGTH, description="Substring search within message text."),
    sender_id: int | None = Query(None, description="Filter by sender ID."),
    date_from: str | None = Query(
        None, max_length=DATE_PARAM_MAX_LENGTH, description="ISO 8601 lower bound on message date."
    ),
    date_to: str | None = Query(
        None, max_length=DATE_PARAM_MAX_LENGTH, description="ISO 8601 upper bound on message date."
    ),
    whole_word: bool = Query(
        False,
        description="When combined with q, match q as a whole word only, not a substring.",
    ),
    order: Literal["asc", "desc"] = Query(
        "desc",
        description="Sort by date ascending (oldest first) or descending (newest first, default).",
    ),
    db: Connection = Depends(get_archive_connection),
) -> dict[str, Any]:
    """
    Return messages within a single chat, newest first by default.

    The `chat` field is omitted from each MessageOut row here - it would be redundant since all messages belong to the same chat_id.

    Supports text search and date range filtering via query parameters.
    """
    # Confirm the chat exists before querying messages.
    if get_chat(db, chat_id) is None:
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} not found.")

    result = get_chat_messages(
        db,
        chat_id=chat_id,
        page=page,
        per_page=per_page,
        q=q,
        sender_id=sender_id,
        date_from=date_from,
        date_to=date_to,
        whole_word=whole_word,
        order=order,
    )
    return _build_page(result, page, per_page)


def _parse_moment(raw: str) -> datetime:
    """
    An ISO 8601 date or datetime from the query string as a timezone-aware UTC datetime.

    A value without an offset ("2026-01-01", "2026-01-01T12:00:00") is read as UTC, not as the server's local time:
    the API must answer the same way wherever it is deployed.
    The web UI always sends an explicit offset.
    Anything unparseable is a 422 the client can translate (reason "invalid_date"); handing the raw string to Postgres instead would be a 500.
    """
    try:
        moment = datetime.fromisoformat(raw.strip())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)  # also rejects offsets that push the date out of range (OverflowError)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "around_date must be an ISO 8601 date or datetime, for example 2026-01-31 or 2026-01-31T12:00:00Z.", "reason": "invalid_date"},
        ) from exc


@router.get(
    "/chats/{chat_id}/window",
    response_model=ChatWindowOut,
    summary="A window of one chat's conversation, addressed by message instead of page number",
)
def get_chat_window_route(
    chat_id: int = _ChatId,
    limit: int = Query(CHAT_WINDOW_DEFAULT, ge=1, le=CHAT_WINDOW_MAX, description="Most messages to return."),
    around: int | None = Query(
        None, ge=MESSAGE_ID_MIN, le=MESSAGE_ID_MAX, description="Internal id of a message: return it with its neighbours on both sides."
    ),
    around_date: str | None = Query(
        None,
        max_length=DATE_PARAM_MAX_LENGTH,
        description="ISO 8601 date or datetime (no offset = UTC): return the window around the first message at or after it "
        "(the last message if the date is later than all of them).",
    ),
    before: int | None = Query(
        None, ge=MESSAGE_ID_MIN, le=MESSAGE_ID_MAX, description="Internal id of a message: return the messages just OLDER than it (scrolling up)."
    ),
    after: int | None = Query(
        None, ge=MESSAGE_ID_MIN, le=MESSAGE_ID_MAX, description="Internal id of a message: return the messages just NEWER than it (scrolling down)."
    ),
    db: Connection = Depends(get_archive_connection),
) -> dict[str, Any]:
    """
    One chat's messages as a window of a conversation, oldest first. Without an anchor it is the newest `limit` messages.

    At most ONE of `around`, `around_date`, `before`, `after` may be given (422 "too_many_anchors" otherwise).
    The cursors are internal message ids the client already holds from earlier responses - they carry no message text,
    so they can safely live in a URL such as `#/chats/<id>/m/<messageId>`.
    Errors: 404 "chat_not_found" for an unknown chat; 404 "message_not_found" when a cursor names a message that is not in THIS chat
    (a message of another chat is deliberately indistinguishable from a message that does not exist).

    Read-only, like every GET here: the connection comes from get_archive_connection(), so a bug in this route cannot modify the archive.
    The reasoning behind keyset windows instead of page numbers is in db/read_queries.get_chat_window().
    """
    if sum(a is not None for a in (around, around_date, before, after)) > 1:
        raise HTTPException(
            status_code=422,
            detail={"message": "Give at most one of: around, around_date, before, after.", "reason": "too_many_anchors"},
        )
    moment = _parse_moment(around_date) if around_date is not None else None

    if get_chat(db, chat_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"Chat {chat_id} not found.", "reason": "chat_not_found"},
        )

    result = get_chat_window(db, chat_id, limit=limit, around=around, around_date=moment, before=before, after=after)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "That message is not in this chat.", "reason": "message_not_found"},
        )
    return result


@router.delete(
    "/chats/{chat_id}",
    response_model=ChatPurgedOut,
    summary="Delete everything stored for one chat (cannot be undone)",
)
def purge_chat(
    chat_id: int = _ChatId,
    current: DecodedAccessToken = Depends(get_current_user),
    conn: Connection = Depends(get_archive_write_connection),
) -> dict[str, Any]:
    """
    Remove a chat from the caller's archive for good: its messages, edit history, deletion snapshots, backfill history and any sender who appears nowhere else.

    This is the owner's "forget this conversation" action, for example for the chat of a Telegram account that no longer exists.
    The caller is expected to have asked for confirmation first; there is no undo and no soft delete.
    What stays: an ingest-blocklist rule for the chat, if there is one (`block_rule_kept`) - see db.queries.purge_chat for why.
    The live archiver may keep running: if the chat receives a new message afterwards it is created again, empty, from that message on.

    Errors: 404 "chat_not_found"; 409 "backfill_running" while this account's backfill runs
    (it could re-insert the history being removed - try again when it has finished).
    Only the CALLER's own archive is reachable (get_archive_write_connection resolves it from the access token), so another account's chat id is simply "not found".
    """
    if is_backfill_running(backfill_status_path(current["user_id"])):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A backfill is running and could store this chat's history again. Wait for it to finish (or cancel it), then try again.",
                "reason": "backfill_running",
            },
        )

    result = purge_chat_data(conn, chat_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"Chat {chat_id} not found.", "reason": "chat_not_found"},
        )
    return result