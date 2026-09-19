"""
Chat-related endpoints:

    GET /api/chats                      — paginated list of all known chats
    GET /api/chats/options              — every chat's {chat_id, name}, unpaginated (for filter dropdowns)
    GET /api/chats/{chat_id}            — single chat with aggregate counts
    GET /api/chats/{chat_id}/messages   — paginated messages within one chat
"""

from sqlalchemy.engine import Connection
from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_archive_connection
from api.schemas import ChatOut, ChatOption, MessageOut, PaginatedResponse
from db.read_queries import get_chat, get_chats, get_chat_options, get_chat_messages

router = APIRouter(tags=["chats"])


def _build_page(result: dict, page: int, per_page: int) -> dict:
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
) -> PaginatedResponse[ChatOut]:
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
def list_chat_options(db: Connection = Depends(get_archive_connection)) -> list[ChatOption]:
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
) -> ChatOut:
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
    q: str | None = Query(None, description="Substring search within message text."),
    sender_id: int | None = Query(None, description="Filter by sender ID."),
    date_from: str | None = Query(
        None, description="ISO 8601 lower bound on message date."
    ),
    date_to: str | None = Query(
        None, description="ISO 8601 upper bound on message date."
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
) -> PaginatedResponse[MessageOut]:
    """
    Return messages within a single chat, newest first by default.

    The `chat` field is omitted from each MessageOut row here — it would be redundant since all messages belong to the same chat_id.

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