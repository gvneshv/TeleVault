"""
The live media download queue: fetches the files of archived messages, one at a time, in the background of the archiver.

Where the work comes from
-------------------------
handlers/on_message.py (and on_edit / backfill) write a `message_media` row with status 'pending' in the same transaction as the message.
That table IS the queue (db/queries.next_pending_media): it survives a restart, a crash and a flood wait, because a job is just a row nobody has settled yet.
This module runs inside the per-account archiver (main.py starts `media_loop` next to the retention job), so it works only while the archiver runs.
A message that arrived while the archiver was stopped is picked up on the next start.

What happens to one job (process_next)
--------------------------------------
1. Refuse before any traffic when Telegram already declares the file too big for the per-file cap ('too_large')
   or when it would push the account past its disk limit ('disk_full').
   The message text is archived either way.
2. Ask Telegram for the message again (we keep ids, not Telegram's file handles, which expire).
   Gone or no longer a file we store: 'unavailable'.
3. Download into `<id>.partial`, then rename onto `<id>`: a half-written file never carries the final name, so a crash cannot leave a "stored" file that is cut short.
   A photo's size is unknown until it is downloaded, so the caps are checked once more on the real size.
4. Record the outcome ('stored', or why not).
   If the message was deleted from the archive meanwhile (the row cascaded away), the new file is removed.

Being gentle with Telegram
--------------------------
Downloading is ordinary client behaviour, but bulk automatic downloads are what Telegram's flood limits exist for,
and its API terms hold the API id responsible for abuse (a restricted api_id would stop EVERY account using it).
So the loop is deliberately slow:
one file at a time, a pause after each real download (PACING_SECONDS), and a FloodWaitError is obeyed to the second (plus a margin) before anything else is asked.
Refusals that cost no traffic need no pause.

Privacy: logs carry ids and counts only - never a chat name, a file name or a message text.
"""

import asyncio
import errno
import logging
import os
from typing import Any, NamedTuple

from telethon.errors import FloodWaitError

import db
import db.queries as queries
from config import settings
from handlers.ingest_filter import is_self_destructing
from handlers.media_filter import media_kind, media_size
from utils.worker_paths import media_dir, media_relative_path

logger = logging.getLogger(__name__)

# How long to wait when the queue is empty before looking again (one indexed single-row read per look).
IDLE_POLL_SECONDS = 10
# The pause after every real download, so a burst of files (an album, a forwarded folder) is spread out instead of hammering Telegram.
PACING_SECONDS = 2.0
# Added to a flood wait: Telegram's number is a minimum, and waking up exactly on it tends to earn a second one.
FLOOD_MARGIN_SECONDS = 1


class Outcome(NamedTuple):
    """What process_next() did with one job."""

    status: str  # what was recorded in message_media.status
    used_telegram: bool  # whether Telegram was asked for anything - only then does the loop pause (refusals from the declared size cost nothing)


def refusal(size: int | None, stored_bytes: int, max_file_bytes: int, account_limit_bytes: int) -> str | None:
    """
    Decide from a file's size alone whether it must NOT be downloaded: 'too_large', 'disk_full' or None (go ahead).
    Pure, so every boundary is tested directly.
    An unknown size (None) is never refused here - it is checked again on the real size.

    The per-file cap wins over the disk limit: "this file is too big for this archive" stays true however much room there is,
    while "disk full" is a state the owner can change by raising the limit.
    A file exactly at a limit is allowed.
    """
    if size is None:
        return None
    if size > max_file_bytes:
        return "too_large"
    if stored_bytes + size > account_limit_bytes:
        return "disk_full"
    return None


def _next_job() -> dict[str, Any] | None:
    with db.get_connection() as conn:
        return queries.next_pending_media(conn)


def _stored_bytes() -> int:
    with db.get_connection() as conn:
        return queries.stored_media_bytes(conn)


def _finish(media_id: int, status: str, **details: Any) -> bool:
    with db.get_connection() as conn:
        return queries.finish_media(conn, media_id, status, **details)


async def _fetch_and_store(client: Any, user_id: int, job: dict[str, Any], limits: tuple[int, int]) -> tuple[str, dict[str, Any]]:
    """
    Steps 2-3: get the message, download the file, and say what to record: (status, extra columns for finish_media).
    Nothing in here touches the database except the disk-usage read; the caller records the outcome.
    A FloodWaitError and a cancellation propagate untouched (nothing is recorded: the job stays pending).
    Any other failure becomes status 'failed'.
    """
    max_file_bytes, account_limit_bytes = limits
    media_id = job["media_id"]
    folder = media_dir(user_id)
    final = folder / str(media_id)
    partial = folder / f"{media_id}.partial"
    try:
        message = await client.get_messages(job["chat_id"], ids=job["tg_message_id"])
        # Gone from Telegram, or no longer a file we store (edited into something else, or timed since): nothing to fetch.
        if message is None or media_kind(message) is None or is_self_destructing(message):
            return "unavailable", {}

        # Declared size, now known for sure (it may have been missing when the message was recorded).
        stored = await asyncio.to_thread(_stored_bytes)
        declared = media_size(message)
        early = refusal(declared, stored, max_file_bytes, account_limit_bytes)
        if early:
            return early, {"size_bytes": declared}

        folder.mkdir(parents=True, exist_ok=True)
        result = await client.download_media(message, file=str(partial))
        if result is None or not partial.exists():
            return "unavailable", {}

        # The real size: the only one a photo ever has, and a check that Telegram's declared size was honest.
        actual = partial.stat().st_size
        late = refusal(actual, stored, max_file_bytes, account_limit_bytes)
        if late:
            return late, {"size_bytes": actual}

        os.replace(partial, final)
        mime_type = getattr(getattr(message, "file", None), "mime_type", None)
        return "stored", {"file_path": media_relative_path(user_id, media_id), "mime_type": mime_type, "size_bytes": actual}
    except (FloodWaitError, asyncio.CancelledError):
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            # The real disk is full, whatever the configured limit says.
            # Not the file's fault, and not worth retrying right now.
            logger.error("The disk is full: media %s was not saved. Free some space (see RUNBOOK.md).", media_id)
            return "disk_full", {}
        logger.warning("Media %s could not be saved (%s).", media_id, type(exc).__name__)
        return "failed", {}
    except Exception as exc:
        logger.warning("Media %s could not be downloaded (%s).", media_id, type(exc).__name__)
        return "failed", {}
    finally:
        # Whatever happened, a half-written file never stays behind.
        # (After a successful rename there is nothing left to remove.)
        partial.unlink(missing_ok=True)


async def process_next(
    client: Any,
    user_id: int,
    *,
    max_file_bytes: int | None = None,
    account_limit_bytes: int | None = None,
) -> Outcome | None:
    """
    Take the oldest pending job and settle it.
    Returns what it recorded, or None when the queue was empty.

    Raises FloodWaitError (the job stays pending, media_loop waits and tries again).
    The limits default to the settings; the parameters exist for tests.
    """
    max_file_bytes = settings.media_max_file_bytes if max_file_bytes is None else max_file_bytes
    account_limit_bytes = settings.media_account_limit_bytes if account_limit_bytes is None else account_limit_bytes

    job = await asyncio.to_thread(_next_job)
    if job is None:
        return None
    media_id = job["media_id"]

    # Step 1: the size Telegram declared when the message arrived.
    # Most refusals end here, without a single request to Telegram.
    early = refusal(job["size_bytes"], await asyncio.to_thread(_stored_bytes), max_file_bytes, account_limit_bytes)
    if early:
        status, details = early, {}
    else:
        status, details = await _fetch_and_store(client, user_id, job, (max_file_bytes, account_limit_bytes))
    used_telegram = not early

    recorded = await asyncio.to_thread(_finish, media_id, status, **details)
    if not recorded:
        # The message was deleted from the archive while we were downloading (its row went with it): nobody refers to the file any more.
        if status == "stored":
            (media_dir(user_id) / str(media_id)).unlink(missing_ok=True)
        logger.info("Media %s was removed from the archive before its download finished; the file was discarded.", media_id)
    else:
        logger.info("Media %s: %s.", media_id, status)
    return Outcome(status, used_telegram)


async def media_loop(client: Any, user_id: int, *, sleep=asyncio.sleep) -> None:
    """
    Work through the queue for as long as the archiver runs.
    Cancelled by main.py on shutdown.

    Never raises for an ordinary problem: a database outage or an unexpected error only skips a pass,
    because archiving messages matters more than downloading their files (the jobs stay pending and are retried).
    `sleep` is injectable so tests do not wait.
    """
    while True:
        try:
            outcome = await process_next(client, user_id)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            wait = int(exc.seconds) + FLOOD_MARGIN_SECONDS
            logger.warning("Telegram asked the media downloader to wait %d s; waiting.", wait)
            await sleep(wait)
            continue
        except Exception as exc:
            logger.error("Media download pass skipped (%s).", type(exc).__name__)
            await sleep(IDLE_POLL_SECONDS)
            continue

        if outcome is None:
            await sleep(IDLE_POLL_SECONDS)
        elif outcome.used_telegram:
            await sleep(PACING_SECONDS)