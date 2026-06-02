"""
Response models for chat-related endpoints.
 
Two levels of detail are provided:
- `ChatOut`     - full chat record, used in the chat list sidebar
- `ChatSummary` - lightweight embed used inside MessageOut to avoid repeating the full chat object for every message in a list
"""

from pydantic import BaseModel, Field


class ChatSummary(BaseModel):
    """
    Minimal chat info embedded inside message responses.
    Avoids sending the full ChatOut (with counts) for every message row.
    """

    chat_id: int
    name: str | None = None
    chat_type: str | None = Field(
        None,
        description="One of: private, group, supergroup, channel.",
    )

    model_config = {"from_attributes": True}


class ChatOut(BaseModel):
    """
    Full chat record returned by GET /api/chats and GET /api/chats/{chat_id}.
 
    `message_count`  - total archived messages in this chat.
    `deleted_count`  - how many of those are flagged as deleted.
    `last_message_at` - ISO 8601 datetime of the most recent archived message,
                        used to sort the chat list by recency (like Telegram itself).
    `last_message_preview` - first 80 chars of the most recent message text,
                             shown in the sidebar. Null if the message had no text.
    """

    chat_id: int
    name: str | None = None
    chat_type: str | None = Field(
        None,
        description="One of: private, group, supergroup, channel.",
    )
    first_seen: str | None = Field(
        None,
        description="ISO 8601 datetime when the app first saw this chat.",
    )
    message_count: int = 0
    deleted_count: int = 0
    last_message_at: str | None = None
    last_message_preview: str | None = Field(
        None,
        max_length=80,
        description="Truncated preview of the most recent message.",
    )

    model_config = {"from_attributes": True}