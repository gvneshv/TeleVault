"""Response and request models for api/routes/senders.py (display-name overrides and name history)."""

import unicodedata
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from utils.validation import DISPLAY_NAME_MAX_COUNT, DISPLAY_NAME_MAX_LENGTH


class DisplayNameIn(BaseModel):
    """
    PUT /senders/{id}/display-name.

    The text is trimmed first, then it must be 1-DISPLAY_NAME_MAX_LENGTH characters: a label of only spaces is an empty label,
    and removing a label is DELETE's job, not "send an empty string".
    Control characters (line breaks, tabs, NUL...) are refused: they would break the layout of a chat bubble header,
    and NUL cannot be stored in a Postgres text column at all (it would surface as a 500).
    Other invisible characters such as the zero-width joiner stay allowed - emoji sequences depend on them.
    """

    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)]

    @field_validator("display_name")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(unicodedata.category(ch) == "Cc" for ch in value):
            raise ValueError("The name must not contain control characters or line breaks.")
        return value


class DisplayNameOut(BaseModel):
    """One sender's label, as stored."""

    sender_id: int
    display_name: str
    updated_at: datetime


class DisplayNameItem(BaseModel):
    """One entry of the label list: just what the UI needs to apply the label."""

    sender_id: int
    display_name: str


class DisplayNamesOut(BaseModel):
    """GET /senders/display-names: every label of the account, so the browser can apply them in one go."""

    items: list[DisplayNameItem]
    total: int
    max_count: int = Field(DISPLAY_NAME_MAX_COUNT, description="The most senders that may carry a label (utils/validation.py's DISPLAY_NAME_MAX_COUNT).")


class DisplayNameRemovedOut(BaseModel):
    """DELETE /senders/{id}/display-name: `removed` is False when the sender had no label (the call is idempotent)."""

    sender_id: int
    removed: bool


class SenderNameVersion(BaseModel):
    """One identity a sender was seen under. `seen_at` is when TeleVault observed it, not when the person changed it (see db/schema.py's sender_name_history)."""

    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    seen_at: datetime | None = None


class SenderDetailOut(BaseModel):
    """
    GET /senders/{id}.

    `username`, `first_name`, `last_name` and `first_seen` are the identity stored when the sender was first seen.
    `history` lists every identity seen, newest first, with the first-seen one as its last entry - so `history[0]` is the latest known name.
    """

    sender_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    first_seen: datetime | None = None
    display_name: str | None = None
    display_name_updated_at: datetime | None = None
    history: list[SenderNameVersion]
