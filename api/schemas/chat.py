"""
Request and response models for chat-related endpoints.
 
Two levels of detail are provided:
- `ChatOut`     - full chat record, used in the chat list sidebar
- `ChatSummary` - lightweight embed used inside MessageOut to avoid repeating the full chat object for every message in a list
- `ChatPurgedOut` - what DELETE /api/chats/{chat_id} reports after removing a chat's stored data
- `UnreadDismissIn` - the request body of POST /api/chats/{chat_id}/unread/dismiss
- `BackfillChatOut` / `BackfillChatsOut` - the chat picker of the backfill dialog (GET /api/backfill/chats)
"""

from datetime import datetime

from pydantic import BaseModel, Field

from utils.validation import TELEGRAM_MESSAGE_ID_MAX, TELEGRAM_MESSAGE_ID_MIN


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


class BackfillChatOut(BaseModel):
    """One chat the backfill dialog may offer: its id (what backfill.py takes as --chat), the name to show, and its kind for grouping."""

    chat_id: int
    # The owner's own label for a private chat's partner when there is one, otherwise Telegram's name. None only for a chat Telegram gave no name.
    name: str | None = None
    chat_type: str


class BackfillChatsOut(BaseModel):
    """
    The backfill's chat picker.
    `available` is False until the account's archiver has published its chat list
    (a fresh install, or an archiver that has not run since the feature arrived) - the picker then offers "All chats" only.
    `updated_at` says how old the list is.
    Blocked chats are never in it.
    """

    available: bool
    updated_at: str | None = None
    chats: list[BackfillChatOut]


class ChatOut(BaseModel):
    """
    Full chat record returned by GET /api/chats and GET /api/chats/{chat_id}.
 
    `message_count`  - total archived messages in this chat.
    `deleted_count`  - how many of those are flagged as deleted.
    `edited_count`   - how many of those were edited at least once (the chat view shows it next to the deleted count).
    `last_message_at` - ISO 8601 datetime of the most recent archived message, used to sort the chat list by recency (like Telegram itself).
    `last_message_preview` - first 80 chars of the most recent message text, shown in the sidebar. Null if the message had no text.
    `read_inbox_max_id` - how far the chat counts as read: Telegram's own position for the owner, or the owner's local "Dismiss" of the unread bar,
                          whichever is further (TeleVault never marks anything as read in Telegram).
    `read_outbox_max_id` - private chats only: how far the OTHER person has read the owner's messages (Telegram's "double check mark");
                           null in groups, channels and when not known yet.
                           The chat view draws the check marks from it.
    `unread_count` - messages after that position that the owner did not write, counted from the archive and capped (the chat list's badge).
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
    read_inbox_max_id: int | None = Field(
        None,
        description="How far this chat counts as read for the account owner: every message with a Telegram id up to it. It is Telegram's own read position, "
        "or the owner's local 'Dismiss' of the unread bar when that is further. The chat view draws its 'Unread messages' bar after it. "
        "Null when not known yet (the archiver has not reported it, or the archive has not been migrated).",
    )

    read_outbox_max_id: int | None = Field(
        None,
        description="Private chats only: every message the owner sent with a Telegram id up to this has been read by the other person "
        "(Telegram's own read_outbox_max_id). Null for groups and channels (never tracked), when not reported yet, or on an archive that has not been migrated.",
    )

    unread_count: int | None = Field(
        None,
        description="How many archived messages of this chat the owner has not read (after read_inbox_max_id, not written by the owner), "
        "at most UNREAD_COUNT_CAP: a value equal to the cap means 'that many or more'. Counted from the archive, so it can be lower than the number "
        "in the Telegram app. Null when it cannot be told (read position or owner id unknown, archive not migrated).",
    )

    model_config = {"from_attributes": True}


class UnreadDismissIn(BaseModel):
    """
    Body of POST /api/chats/{chat_id}/unread/dismiss: the owner pressed "Dismiss" on the unread bar.

    `up_to` - the Telegram id of the newest message the page is showing; everything up to it counts as read from now on, in TeleVault only.
    The server clamps it to the newest archived message of the chat, so a larger number cannot hide messages that have not arrived yet.
    """

    up_to: int = Field(ge=TELEGRAM_MESSAGE_ID_MIN, le=TELEGRAM_MESSAGE_ID_MAX, description="Telegram id of the newest message the owner has in front of them.")


class ChatPurgedOut(BaseModel):
    """
    What was removed by DELETE /api/chats/{chat_id}. Counts only - the response never repeats any of the removed content.

    `block_rule_kept` is true when the chat was on the ingest blocklist and the rule is still there:
    that rule is NOT removed unless the request asked for it (`also_unblock`, see db.queries.purge_chat),
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