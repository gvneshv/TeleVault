"""
Downloaded media, for the CALLING account only.

    POST /api/media/retry  - put files that were refused or failed back in the download queue
    GET  /api/media/policy - which file types are stored (Settings -> Files)
    PUT  /api/media/policy - change that

Why it exists:
    A file is refused ('too_large', 'disk_full') or breaks ('failed') once, and nothing retries it by itself (media_downloader.py: refused and failed rows are terminal).
    After the owner raised MEDIA_MAX_FILE_MB or MEDIA_ACCOUNT_LIMIT_GB, or freed disk space, this is the way to give those files another chance.

What it does NOT do:
    It downloads nothing.
    The rows become 'pending' and the account's live archiver (main.py) works through them at its own gentle pace,
    so a file that is still over a limit is refused again without a request to Telegram (the declared size is kept).
    'unavailable' files are never requeued - Telegram no longer has them - and a stored file is never touched.
    Nothing is deleted or overwritten; the message text is not involved at all.

The file-type policy:
    One account-wide choice - photo, video, voice, audio, document, each on or off - applies to the live archiver and to every backfill alike.
    A type that is off is still RECORDED (the chat view says "Photo") but never downloaded;
    saving applies at once to files already waiting ('pending' <-> 'skipped', db.queries.save_media_policy).
    Files already stored are never deleted by switching a type off.
    Everything is on by default. 409 "archive_outdated" on an archive that has not been migrated (scripts/migrate_archives.py).

Writes go through api.dependencies.get_archive_write_connection, which resolves the CALLER's own archive: one account can never requeue another's files.
An archive without the message_media table (not migrated yet) simply has nothing to requeue and answers 0.
"""

from contextlib import contextmanager
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import db
from api.dependencies import get_archive_connection, get_archive_write_connection
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


class MediaPolicyIn(BaseModel):
    """Which file types to store.
    Exactly these five switches, so the body can never grow."""

    photo: bool
    video: bool
    voice: bool
    audio: bool
    document: bool


class MediaPolicyOut(MediaPolicyIn):
    # Only set by PUT: how many WAITING files the change affected (turned to 'skipped' / put back in the queue). 0 on GET.
    skipped: int = 0
    queued: int = 0


@contextmanager
def _needs_policy_table() -> Iterator[None]:
    """Turn "the table does not exist" (an archive not migrated yet) into a 409 the page can explain, like the retention card does."""
    try:
        yield
    except ProgrammingError as exc:
        if not db.is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database needs updating before file types can be chosen. Run `python scripts/migrate_archives.py` - see RUNBOOK.md.",
                "reason": "archive_outdated",
            },
        ) from exc


@router.get("/policy", response_model=MediaPolicyOut)
def get_media_policy(conn: Connection = Depends(get_archive_connection)):
    """Which file types are stored right now (all of them until the owner changes it)."""
    with _needs_policy_table():
        policy = db.queries.get_media_policy(conn)
        conn.rollback()
    return policy


@router.put("/policy", response_model=MediaPolicyOut)
def put_media_policy(body: MediaPolicyIn, conn: Connection = Depends(get_archive_write_connection)):
    """
    Save the choice.
    Takes effect for new files at once;
    the files already waiting are re-sorted in the same transaction (see the module docstring), and the counts say how many that was.
    """
    enabled = [kind for kind, on in body.model_dump().items() if on]
    with _needs_policy_table():
        changed = db.queries.save_media_policy(conn, enabled)
    return {**body.model_dump(), **changed}
