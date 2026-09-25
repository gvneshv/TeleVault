"""Response model for api/routes/archive.py."""

from pydantic import BaseModel


class ArchiveProvisionOut(BaseModel):
    """Returned by POST /archive/provision on success."""

    archive_db_ref: str