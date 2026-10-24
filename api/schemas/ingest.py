"""Response models for api/routes/ingest.py (the ingestion-time chat blocklist)."""

from datetime import datetime

from pydantic import BaseModel, Field


class BlockedChatOut(BaseModel):
    """One chat the account owner chose not to archive."""

    chat_id: int
    name: str | None = Field(
        None,
        description="The chat's name when it was blocked. Kept on the rule itself, so it is still shown after the chat's own data is gone.",
    )
    blocked_at: datetime


class BlocklistOut(BaseModel):
    """GET /ingest/blocklist."""

    items: list[BlockedChatOut]
    total: int
    max_count: int = Field(description="The most chats one account may block (utils/validation.py's INGEST_BLOCKLIST_MAX_COUNT).")


class BlockedChatRemovedOut(BaseModel):
    """DELETE /ingest/blocklist/{chat_id}: `removed` is False when the chat was not blocked in the first place (the call is idempotent)."""

    chat_id: int
    removed: bool
