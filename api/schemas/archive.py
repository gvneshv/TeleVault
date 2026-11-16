"""Response model for api/routes/archive.py."""

from datetime import datetime

from pydantic import BaseModel, Field

from utils.validation import RETENTION_COUNT_MAX, RETENTION_MAX_MONTHS, RETENTION_MIN_MONTHS


class ArchiveProvisionOut(BaseModel):
    """Returned by POST /archive/provision on success."""

    archive_db_ref: str


class ArchiveOwnerOut(BaseModel):
    """Returned by GET /archive/owner."""

    owner_id: int | None = Field(
        None,
        description="Telegram user id of the account this archive belongs to (compare with a message's sender.sender_id); "
        "null while it is not known - the archiver has not recorded it yet, or the archive predates the migration.",
    )



class ArchiveClearedOut(BaseModel):
    """
    What DELETE /api/archive/data removed.
    Counts only - the response never repeats any of the removed content.

    `senders_kept` is how many people were NOT removed because the owner gave them a display name (their label hangs on that row);
    they keep just the name they were first seen under, and no name history.
    """

    messages_deleted: int
    edits_deleted: int
    deletions_deleted: int
    chats_deleted: int
    senders_deleted: int
    senders_kept: int
    backfill_runs_deleted: int

class RetentionIn(BaseModel):
    """
    Body of PUT /api/archive/retention: the owner's "delete archived messages older than N months" setting.

    `confirmed_messages` is the number the preview showed (POST /api/archive/retention/preview).
    It is REQUIRED to switch the policy on:
    the server counts again and refuses to save if clearly more would be deleted than the owner confirmed (409 `preview_outdated`).
    Ignored when `enabled` is false.
    """

    enabled: bool
    months: int = Field(ge=RETENTION_MIN_MONTHS, le=RETENTION_MAX_MONTHS, description="Whole calendar months; messages SENT longer ago than this are deleted.")
    keep_deleted: bool = Field(True, description="True: messages that were deleted in Telegram are never removed by the policy.")
    confirmed_messages: int | None = Field(None, ge=0, le=RETENTION_COUNT_MAX, description="How many messages the owner saw in the preview.")


class RetentionPreviewIn(BaseModel):
    """Body of POST /api/archive/retention/preview: the settings to count for. Nothing is changed or deleted."""

    months: int = Field(ge=RETENTION_MIN_MONTHS, le=RETENTION_MAX_MONTHS)
    keep_deleted: bool = True


class RetentionPreviewOut(BaseModel):
    """
    What the policy would delete RIGHT NOW with the given settings.
    Counts and dates only - never any message content.
    `oldest` / `newest` are the dates of the oldest and newest message that would go (null when none); `total` is the whole archive.
    """

    count: int
    total: int
    oldest: datetime | None = None
    newest: datetime | None = None
    cutoff: datetime


class RetentionOut(BaseModel):
    """
    The saved policy and the outcome of its last run (GET and PUT /api/archive/retention).

    `configured` is false until the owner saves a policy for the first time; the other fields are then the defaults (off, no number).
    `last_run_status`: 'ok', 'blocked' (it would have deleted more than the limit - nothing was deleted; save again after a new preview) or 'error'.
    The job runs only while the archiver runs; `last_run_at` shows when it last did.
    """

    configured: bool
    enabled: bool = False
    months: int | None = None
    keep_deleted: bool = True
    confirmed_messages: int | None = None
    updated_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_run_deleted: int | None = None
    min_months: int = RETENTION_MIN_MONTHS
    max_months: int = RETENTION_MAX_MONTHS
