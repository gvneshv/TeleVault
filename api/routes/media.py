"""
Downloaded media, for the CALLING account only.

    POST /api/media/retry  - put files that were refused or failed back in the download queue
    GET  /api/media/policy - which file types are stored (Settings -> Files)
    PUT  /api/media/policy - change that
    GET  /api/media/usage  - what the stored files take and how many wait / were refused (Settings -> Files)
    GET  /api/media/file/{message_id} - the stored file of one message, for the chat view

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

Serving a file:
    The chat view fetches it with the normal bearer token (an <img src> cannot send one) and shows it from a blob.
    The caller's OWN archive decides what exists (a message id of another account simply has no row: 404),
    the path must have the exact shape the downloader writes (utils.media_files.resolve_media_file), and the type is sniffed from the file's bytes.
    Only a short allow-list (common image, video, audio and PDF formats) is shown inline;
    anything else is sent as `application/octet-stream` with `Content-Disposition: attachment`, plus `nosniff` and a sandboxing CSP on every response,
    so a hostile file archived from a chat can never run in the app's origin.
    The original file name is not kept by the archive, so downloads are named `televault-<id>.<ext>`.

Writes go through api.dependencies.get_archive_write_connection, which resolves the CALLER's own archive: one account can never requeue another's files.
An archive without the message_media table (not migrated yet) simply has nothing to requeue and answers 0.
"""

from contextlib import contextmanager
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import db
from db import read_queries
from api.dependencies import DecodedAccessToken, get_archive_connection, get_archive_write_connection, get_current_user
from config import settings
from db.queries import RETRYABLE_MEDIA_STATUSES
from utils.media_files import resolve_media_file, sniff_media_type

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


class MediaUsageOut(BaseModel):
    stored_files: int
    stored_bytes: int
    # The per-account ceiling (MEDIA_ACCOUNT_LIMIT_GB) and the per-file one (MEDIA_MAX_FILE_MB), so the page can draw the bar and explain "too large".
    limit_bytes: int
    max_file_bytes: int
    # Every status, zero when none: pending, stored, too_large, disk_full, unavailable, failed, skipped.
    counts: dict[str, int]


@router.get("/usage", response_model=MediaUsageOut)
def get_media_usage(conn: Connection = Depends(get_archive_connection)):
    """What the account's downloaded files take, against the limits, and how many files wait or were refused."""
    return {
        **read_queries.get_media_usage(conn),
        "limit_bytes": settings.media_account_limit_bytes,
        "max_file_bytes": settings.media_max_file_bytes,
    }


@router.get("/file/{message_id}")
def get_media_file(
    message_id: int,
    conn: Connection = Depends(get_archive_connection),
    current: DecodedAccessToken = Depends(get_current_user),
):
    """The stored file of one message (see the module docstring for how it is served).
    404 `media_not_stored` for every case with nothing to send."""
    if not 0 < message_id < 2**63:
        raise HTTPException(status_code=422, detail={"message": "Invalid message id.", "reason": "invalid_id"})
    record = read_queries.get_media_file_record(conn, message_id)
    path = resolve_media_file(current["user_id"], record["file_path"]) if record and record["status"] == "stored" else None
    if record is None or path is None:
        raise HTTPException(status_code=404, detail={"message": "This message has no stored file.", "reason": "media_not_stored"})
    content_type, extension, inline = sniff_media_type(path, record["kind"])
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="televault-{message_id}.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=3600",
        },
    )