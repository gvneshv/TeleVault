"""
Response models for GET /api/stats.
 
Designed to feed a dashboard view: global totals at the top, then a per-chat breakdown table below. 
All counts are integers; percentages are computed on the frontend from the raw numbers to avoid float precision noise in the API.
"""

from pydantic import BaseModel, Field


class ChatStatRow(BaseModel):
    """
    One row in the per-chat stats breakdown table.
    """

    chat_id: int
    name: str | None = None
    chat_type: str | None
    message_count: int
    deleted_count: int
    edited_count: int
    first_message_at: str | None = Field(None, description="ISO 8601 timestamp of the earliest archived message.")
    last_message_at: str | None = Field(None, description="ISO 8601 timestamp of the most recent archived message.")

    model_config = {"from_attributes": True}


class StatsOut(BaseModel):
    """
    Global archive statistics returned by GET /api/stats.
 
    `archiving_since` - datetime of the very first archived message across all chats.
    Gives a quick sense of how long TeleVault has been running.
    """

    total_messages: int
    total_deleted: int
    total_edited: int
    total_chats: int
    total_senders: int
    archiving_since: str | None = Field(None, description="ISO 8601 datetime of the earliest archived message.")
    per_chat: list[ChatStatRow] = Field(
        default_factory=list,
        description="Per-chat breakdown, sorted by message_count descending",
    )