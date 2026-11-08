"""Response model for api/routes/archive.py."""

from pydantic import BaseModel, Field


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