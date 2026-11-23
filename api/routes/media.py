"""
Downloaded media, for the CALLING account only.
Today one route: try the refused and failed files again.

    POST /api/media/retry  - put files that were refused or failed back in the download queue

Why it exists:
    A file is refused ('too_large', 'disk_full') or breaks ('failed') once, and nothing retries it by itself (media_downloader.py: refused and failed rows are terminal).
    After the owner raised MEDIA_MAX_FILE_MB or MEDIA_ACCOUNT_LIMIT_GB, or freed disk space, this is the way to give those files another chance.

What it does NOT do:
    It downloads nothing.
    The rows become 'pending' and the account's live archiver (main.py) works through them at its own gentle pace,
    so a file that is still over a limit is refused again without a request to Telegram (the declared size is kept).
    'unavailable' files are never requeued - Telegram no longer has them - and a stored file is never touched.
    Nothing is deleted or overwritten; the message text is not involved at all.

Writes go through api.dependencies.get_archive_write_connection, which resolves the CALLER's own archive: one account can never requeue another's files.
An archive without the message_media table (not migrated yet) simply has nothing to requeue and answers 0.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

import db
from api.dependencies import get_archive_write_connection
from db.queries import RETRYABLE_MEDIA_STATUSES

router = APIRouter(prefix="/media", tags=["media"])

RetryableStatus = Literal["too_large", "disk_full", "failed"]


class MediaRetryIn(BaseModel):
    # Which outcomes to retry; by default all of them. At most one entry per retryable status, so the list can never grow.
    statuses: list[RetryableStatus] = Field(default_factory=lambda: list(RETRYABLE_MEDIA_STATUSES), max_length=len(RETRYABLE_MEDIA_STATUSES))


class MediaRetryOut(BaseModel):
    requeued: int


@router.post("/retry", response_model=MediaRetryOut)
def retry_media(body: MediaRetryIn, conn: Connection = Depends(get_archive_write_connection)):
    """Requeue the account's refused/failed files (see the module docstring).
    Returns how many were put back."""
    return {"requeued": db.queries.requeue_media(conn, body.statuses)}
