"""
Response models for chat-related endpoints.
 
Two levels of detail are provided:
- `ChatOut`     - full chat record, used in the chat list sidebar
- `ChatSummary` - lightweight embed used inside MessageOut to avoid repeating the full chat object for every message in a list
- `ChatPurgedOut` - what DELETE /api/chats/{chat_id} reports after removing a chat's stored data
"""

from datetime import datetime

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


class ChatOption(BaseModel):
    """
    Minimal {chat_id, name} pair used to populate the chat-filter dropdown on the Messages/Deleted views (GET /api/chats/options).
    Deliberately smaller than ChatSummary - the dropdown only ever displays a name, never a type badge.
    """

    chat_id: int
    name: str | None = None

    model_config = {"from_attributes": True}


class ChatOut(BaseModel):
    """
    Full chat record returned by GET /api/chats and GET /api/chats/{chat_id}.
 
    `message_count`  - total archived messages in this chat.
    `deleted_count`  - how many of those are flagged as deleted.
    `edited_count`   - how many of those were edited at least once (the chat view shows it next to the deleted count).
    `last_message_at` - ISO 8601 datetime of the most recent archived message, used to sort the chat list by recency (like Telegram itself).
    `last_message_preview` - first 80 chars of the most recent message text, shown in the sidebar. Null if the message had no text.
    """

    chat_id: int
    name: str | None = None
    chat_type: str | None = Field(
        None,
        description="One of: private, group, supergroup, channel.",
    )
    first_seen: datetime | None = Field(
        None,
        description="ISO 8601 datetime when the app first saw this chat.",
    )
    message_count: int = 0
    deleted_count: int = 0
    edited_count: int = Field(0, description="How many of those messages were edited at least once.")
    last_message_at: datetime | None = None
    last_message_preview: str | None = Field(
        None,
        max_length=80,
        description="Truncated preview of the most recent message.",
    )

    model_config = {"from_attributes": True}


class ChatPurgedOut(BaseModel):
    """
    What was removed by DELETE /api/chats/{chat_id}.
    Counts only - the response never repeats any of the removed content.

    `block_rule_kept` is true when the chat is on the ingest blocklist: that rule is deliberately NOT removed (see db.queries.purge_chat),
    so the UI can say "still not archived" instead of implying the chat is simply gone.
    `senders_deleted` counts only people who wrote in no other chat of the archive.
    """

    chat_id: int
    messages_deleted: int
    edits_deleted: int
    deletions_deleted: int
    senders_deleted: int
    backfill_runs_deleted: int
    block_rule_kept: bool