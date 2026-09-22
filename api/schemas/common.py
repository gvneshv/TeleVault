"""
Shared building blocks used across multiple response schemas
"""

from typing import Generic, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """
    Generic pagination envelope returned by all list endpoints.
 
    Example response shape:
        {
            "items": [...],
            "total": 1024,
            "page": 2,
            "per_page": 50,
            "pages": 21
        }
 
    The frontend uses `total` and `pages` to render pagination controls without needing a separate count request.
    """

    items: list[T]
    total: int = Field(..., description="Total number of matching records.")
    page: int = Field(..., ge=1, description="Current page number.")
    per_page: int = Field(..., ge=1, le=200, description="Records per page.")
    pages: int = Field(..., description="Total number of pages.")


class HealthOut(BaseModel):
    """
    Liveness response from GET /api/health.

    `db_readable` confirms the relevant database (see `archive_status`) is accessible and returns rows.
    `session_exists` confirms the Telethon .session file is present on disk AND this caller is the instance owner (see `is_instance_owner`) -
    it does NOT mean the userbot is currently connected to Telegram (that would require IPC, which is out of scope for Phase 2).
    """

    status: str = Field(..., description="'ok' or 'degraded'.")
    archive_status: str = Field(
        ...,
        description=(
            "'ok' (the relevant database was read successfully), 'unattached' (this account has no "
            "archive_db_ref yet - logged-in callers only), or 'unavailable' (a database reference "
            "exists but couldn't be reached right now)."
        ),
    )
    db_readable: bool
    is_instance_owner: bool = Field(
        ...,
        description=(
            "Whether this account is the one whose archive_db_ref matches this running instance's own "
            "database_url - the same 'instance owner' concept require_instance_owner() checks for "
            "/telethon/* and /backfill/*. There is exactly one physical Telethon session per instance "
            "today, and it belongs to this one account, not to 'the instance' in the abstract."
        ),
    )
    session_exists: bool
    db_message_count: int | None = Field(
        None, description="Quick sanity check - total rows in messages table. None when archive_status != 'ok'."
    )