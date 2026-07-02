"""
Response models for message-related endpoints.
 
Model hierarchy:
    SenderOut           - sender identity, embedded in messages
    EditOut             - one entry in a message's edit history
    DeletionOut         - deletion record with inference metadata
    MessageOut          - one row in a paginated message list
    MessageDetail       - full message with edit history + deletion record, returned by GET /api/messages/{id}
"""

from typing import Literal
from pydantic import BaseModel, Field

from .chat import ChatSummary


class SenderOut(BaseModel):
    """
    Sender identity embedded inside message responses.
 
    `display_name` is the user-defined label override (Phase 3 feature).
    When present it takes precedence over first_name/last_name in the UI.
    The field is included now so the API contract is stable before the feature is built - the backend simply returns None until then.
    """

    sender_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = Field(None, description="User-defined label override for this sender. Phase 3 feature.")

    @property
    def resolved_name(self) -> str:
        """
        Best human-readable name available, in priority order:
        display_name -> first+last -> first -> @username -> str(sender_id)
        """
        if self.display_name:
            return self.display_name
        parts = filter(None, [self.first_name, self.last_name])
        full = " ".join(parts)
        if full:
            return full
        if self.username:
            return f"@{self.username}"
        return str(self.sender_id)
    
    model_config = {"from_attributes": True}


class EditOut(BaseModel):
    """
    One record from the message_edits table.
    Returned as a list inside MessageDetail.
    """

    id: int
    old_text: str | None = None
    new_text: str | None = None
    edited_at: str | None = Field(None, description="ISO 8601 datetime of the edit")

    model_config = {"from_attributes": True}


# Inference values for who deleted a message.
# See db/queries.py flag_deleted() for the inference logic and its documented limitations - this is a best-effort guess, not a reliable fact.
DeletionActorInference = Literal["self", "other", "unknown"]


class DeletionOut(BaseModel):
    """
    Deletion record for a message, embedded in MessageDetail.
 
    `deleted_by_inference` is a best-effort guess based on sender_id and
    timing, valid only for private chats. Always treat it as informational.
 
    Possible values:
      'self'    - the authenticated TeleVault user likely deleted this message
      'other'   - the other party likely deleted it ("Delete for everyone")
      'unknown' - could not be inferred (group chat, or insufficient data)
 
    `inference_confidence` gives a plain-language note shown in the UI tooltip
    so the user understands why the inference may be wrong.
    """

    id: int
    text_snapshot: str | None = Field(None, description="Copy of the message text at time of deletion")
    deleted_at: str | None = Field(None, description="ISO 8601 datetime when the deletion was detected")
    deleted_by_inference: DeletionActorInference = "unknown"
    inference_confidence: str | None = Field(
        None,
        description="Human-readable note explaining the basis and limitations of the deletion actor inference. Displayed as a UI tooltip."
    )

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    """
    One row in a paginated message list.
 
    Embeds sender and chat inline to avoid N+1 lookups from the frontend.
    `chat` is None when returned from a per-chat endpoint (redundant there).
    """

    id: int = Field(..., description="Internal app's message ID.")
    tg_message_id: int = Field(..., description="Telegram's own message ID.")
    chat: ChatSummary | None = Field(None, description="Omitted on per-chat endpoints where it would be redundant")
    sender: SenderOut | None = None
    text: str | None = None
    date: str | None = Field(..., description="ISO 8601 send timestamp.")
    archived_at: str | None = Field(..., description="ISO 8601 timestamp when the app first stored this message")
    is_edited: bool = False
    edited_at: str | None = None
    is_deleted: bool = False
    deleted_at: str | None = None

    model_config = {"from_attributes": True}


class MessageDetail(BaseModel):
    """
    Full message record including edit history and deletion details. 
    Returned by GET /api/messages/{id}.
 
    Extends MessageOut so the frontend can reuse the same type for both list rows and the detail view with a simple field check.
    """

    edits: list[EditOut] = Field(
        default_factory=list,
        description="Full edit history, ordered oldest -> newest.",
    )
    deletion: DeletionOut | None = Field(None, description="Deletion record. None if the message has not been deleted")