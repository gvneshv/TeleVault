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

