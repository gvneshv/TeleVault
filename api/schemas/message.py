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
# STATUS (as of this writing): not yet implemented.
# Every row currently returns 'unknown' via message_deletions' column DEFAULT — no code computes 'self'/'other' anywhere in the backend
# (checked: neither flag_deleted() in db/queries.py nor handlers/on_delete.py compute or write this value;
# the older comment pointing to flag_deleted() for "the inference logic" was aspirational, not accurate).
# If this is implemented later, treat it as a best-effort guess, never a reliable fact — Telegram's API does not expose who performed a deletion.
DeletionActorInference = Literal["self", "other", "unknown"]


class DeletionOut(BaseModel):
    """
    Deletion record for a message, embedded in MessageDetail.

    `deleted_by_inference` is currently always 'unknown' — see the STATUS note on DeletionActorInference above.
    The field and the three possible values below describe a planned feature, not current behavior.

    Possible values (once/if implemented):
      'self'    - the authenticated TeleVault user likely deleted this message
      'other'   - the other party likely deleted it ("Delete for everyone")
      'unknown' - could not be inferred, or not yet computed (current state for all rows)

    Note on where any future inference would actually be reliable:
    for channels, if the archiving account isn't an admin, only channel admins can delete posts — so any deletion there is deterministically 'other', not a guess.
    Private/group chats are the opposite:
    Telegram allows either party to delete a message for everyone with no time limit and no trace of who did it,
    so a sender_id/timing-based guess there would be closer to a coin flip than a signal.
    Worth keeping these cases separate rather than one shared "best-effort" implementation for all chat types.

    `inference_confidence` gives a plain-language note shown in the UI so the user understands the basis (or absence) of the inference.
    """

    id: int
    text_snapshot: str | None = Field(None, description="Copy of the message text at time of deletion")
    deleted_at: str | None = Field(None, description="ISO 8601 datetime when the deletion was detected")
    deleted_by_inference: DeletionActorInference = "unknown"
    inference_confidence: str | None = Field(
        None,
        description="Human-readable note explaining the basis and limitations of the deletion actor inference. Currently always null — see DeletionOut's docstring."
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