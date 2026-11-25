"""
One-off / on-demand script to archive historical messages - the ones sent before TeleVault was running, or before a chat was ever seen by it.

Run with (normally started for you by the web UI's Backfill tab - POST /api/backfill/start - which passes --user-id itself):
    python backfill.py --user-id 1                      # every chat the account is in
    python backfill.py --user-id 1 --chat @someusername  # one chat only
    python backfill.py --user-id 1 --chat -1001234567890 # one chat by numeric ID
    python backfill.py --user-id 1 --chat @someusername --limit 500  # cap per chat, useful for testing
    python backfill.py --user-id 1 --full                # force a full re-walk of every chat, ignoring what's already archived
    python backfill.py --user-id 1 --mode text           # message text only (no files);  --mode media = only messages that carry a file

--user-id is the control-database id of the account (see the admin panel).
The account's Telegram login and archive database are read from the control database - nothing Telegram-related lives in .env any more (see utils/account.py).

Incremental by default:
    Each chat only fetches messages newer than the point an earlier backfill already walked to (chats.backfill_covered_id,
    see db.queries.get_backfill_covered_id), which is passed to Telethon as min_id.
    A chat that was never backfilled gets a full walk - this only skips *already-covered* history on repeat runs.
    Telegram message IDs only grow within a chat, so one number per chat is a safe marker, not a guess.

    The marker is deliberately NOT "the highest message id we hold": the live archiver stores messages too,
    so a chat first seen live at message 1000 would have been treated as covered up to 1000 and its older history never fetched.
    The marker moves only here, in the same transaction as the messages it covers, and also when a run is cancelled partway (it keeps what was done).

    Pass --full to ignore the marker and re-walk everything regardless - useful if you suspect gaps in an existing archive and want to re-verify it from scratch.
    ON CONFLICT DO NOTHING still makes this safe to run (see below), just slower and heavier on Telegram's API than it needs to be for routine use.
    An archive that predates the column cannot remember anything, so it is walked in full every time (one warning; `python scripts/migrate_archives.py` fixes it).

Pacing - how gently this talks to Telegram:
    - Pages of history (100 messages each) are requested with a pause between them (PAGE_WAIT_SECONDS, Telethon's wait_time).
      Telethon would add 1 s on its own only for walks over 3000 messages; passing it explicitly makes every walk equally unhurried.
    - Between two chats that were really fetched, a RANDOM pause of CHAT_PAUSE_SECONDS (2-5 s) - never a fixed interval,
      which looks mechanical, and a run over hundreds of chats should not hit Telegram in a metronome pattern.
      Blocked chats cost no request, so they cost no pause either.
    - Both pauses end early when the run is cancelled.

Cooldown - one backfill per period, for every chat:
    After a run COMPLETES, the next one may start only after BACKFILL_COOLDOWN_HOURS (default 24, 0 = off, at most 168; config.py).
    It is measured from the end of the account's last completed run (backfill_runs), whatever chat that run covered, text and media alike.
    A run that was cancelled or stopped by a flood wait does not start it, so picking such a run up again is "the same run, resumed" and is always allowed.
    So is a run in which a chat was given up on after repeated flood waits: it is recorded as an error, not as completed.
    Enforced here (the one entry point, like the running-backfill check) and again by the web API, which can then say so before spawning anything.

What to fetch (--mode):
    all   - messages and their files (the default).
    text  - message text only.
            A file is still RECORDED (so the chat view can say "Photo") but as 'skipped', never queued; a message with no text is not stored.
    media - only messages that carry a file; others are passed over.
            The files go to the download queue.
    Which FILE TYPES are downloaded is not a per-run choice: Settings -> Files decides it for the whole account (live archiver and every backfill alike),
    and a type that is switched off is recorded as 'skipped' whatever the mode.
    Each kind of walk has its own marker (_resume_min_id): text -> chats.backfill_covered_id, media -> chats.media_walked_id, "all" -> the lower of the two.
    So a text-only run followed by a media run is two cheap walks, never one skipped by the other's progress.
    "text" and "media" refuse to start on an archive without migration e2b7c4d9a613; "all" always works.

Files of already archived messages:
    A message archived before media archiving existed is a text-only row: nothing recorded that it also carried a file.
    The file marker starts empty for every chat after the migration, so the first "all" or "media" walk of a chat meets those messages again:
    when one that is already archived carries a file it gets its media row (db.queries.add_missing_media_row)
    and the live downloader fetches the file the next time the archiver runs.
    Nothing is downloaded here, and a media row that already exists is never reset.
    (--full does the same walk on demand, ignoring both markers.)

Flood waits (Telegram's "slow down, wait N seconds"):
    - Up to 60 s (flood_sleep_threshold, utils/telegram_client.py) Telethon sleeps by itself and carries on; nothing here sees it.
    - Longer waits reach this script as FloodWaitError.
      Every request it makes is covered - the chat list, a --chat lookup, the migration probe and the history itself:
      the run waits as long as Telegram asked (plus a second), tells the web UI (status file: flood_wait_seconds) and retries, up to MAX_FLOOD_RETRIES times.
    - A wait longer than MAX_FLOOD_WAIT_SECONDS (an hour) is not slept through: the run stops with state "error" and says so; start it again later.
      Pressing on with other chats while the account is throttled would only make Telegram's limits harsher.
    - Cancel works during a wait.

Important limitations - Telegram's API, not something TeleVault can work around:
  - Deleted messages cannot be backfilled.
    Telegram's history API only returns messages that currently exist in a chat.
    If a message was deleted before TeleVault started archiving that chat, that history is gone - there is no way to recover it.
  - Edit history cannot be backfilled either.
    Only the CURRENT (latest) text of each historical message is available,
    so backfilled messages are stored as not-yet-edited (is_edited=0) even if they actually were edited before backfill ran.
    There's no way to see prior versions of a message you didn't already have archived.

Blocked chats are skipped:
    A chat on the account's ingest blocklist (Settings -> Blocked chats) is not fetched and not stored -
    the same rule the live archiver applies (handlers/ingest_filter.py).
    It is checked per chat, before any message of it is requested from Telegram.
    That also holds when one is named with --chat: the run says so and archives nothing, rather than quietly overriding the owner's rule.
    Remove the chat from the blocklist first if you do want it backfilled.

Self-destructing messages are skipped:
    A message with an auto-delete timer or view-once media is not stored, exactly as in the live archiver (handlers/ingest_filter.py, which explains why).

Message ignore rules apply to history too:
    A plain-text message whose text matches a rule (Settings -> Ignore rules) is not stored,
    exactly as in the live archiver (handlers/ingest_filter.py) - otherwise every backfill would bring back the very messages the rule keeps out.
    The rules that apply to a chat are read once, before its first message; messages already archived are never removed by a rule.

Do not run this at the same time as main.py (the live userbot) FOR THE SAME ACCOUNT - Telethon sessions support one active connection at a time.
Stop that account's archiver first, run this, then start it again. (Other accounts' archivers are unaffected.)

Idempotent: insert_message() uses ON CONFLICT DO NOTHING (see db/queries.py), so running this more than once,
or interrupting it partway through and re-running later, is always safe - already-archived messages are silently skipped, never duplicated.
Incremental mode above is what makes repeat runs *cheap* too, not just safe - idempotency alone doesn't save you from re-fetching everything over the wire.
"""

import argparse
import asyncio
from collections import Counter
import logging
import json
import os
import random
import signal
import sys
import time

from pathlib import Path
from typing import Any

from datetime import datetime, timezone

from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChat
from sqlalchemy import text as sql_text

import db
from config import settings
from utils.logging_setup import setup_logging
from utils.text_matching import matches_any
from utils.atomic_write import atomic_write_json
from handlers.helpers import get_chat_type, get_sender_fields, resolve_message_text
from handlers.ingest_filter import is_self_destructing, matchable_text
from handlers.media_filter import media_kind, media_size
from api.process_utils import is_archiver_running, is_backfill_running
from utils.account import AccountContextError, load_account_context
from utils.telegram_client import client_options
from utils.telethon_compat import disconnect_client, get_authenticated_user
from utils.worker_paths import backfill_status_path, heartbeat_path

logger = logging.getLogger(__name__)

# Log a progress line every this many messages processed within a single chat - large channels/years of history can take a long time,
# and a completely silent script for that long looks hung even when it isn't.
PROGRESS_INTERVAL = 500

# Pause between two pages of 100 messages of one chat (Telethon's wait_time).
# See "Pacing" in the module docstring.
PAGE_WAIT_SECONDS = 1

# Random pause (min, max seconds) between two chats that were really fetched.
# Random on purpose - see "Pacing".
CHAT_PAUSE_SECONDS = (2.0, 5.0)

# How often one request may be retried after a FloodWaitError before that chat (or the chat list) is given up on.
MAX_FLOOD_RETRIES = 5

# A flood wait longer than this is not slept through - the run stops instead (see "Flood waits" in the module docstring).
MAX_FLOOD_WAIT_SECONDS = 3600

# This run's status file (utils/worker_paths.py) - set once in main(), once --user-id is known.
_status_file: Path | None = None


def _write_status(data: dict) -> None:
    """
    Overwrite the backfill status file.
    Read by GET /api/backfill/status so the web UI can render progress without any direct coupling to this process beyond this one file.
    """
    assert _status_file is not None, "main() sets _status_file before run() is ever called"
    atomic_write_json(_status_file, data)


_cancelled = False


class _FloodWaitStop(Exception):
    """Telegram asked for a wait this run will not sit through (too long, or retried too often): the run ends here with state "error"."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram asked to wait {seconds}s")
        self.seconds = seconds


async def _sleep_unless_cancelled(seconds: float) -> None:
    """
    asyncio.sleep() that ends early when the run is cancelled (SIGTERM from the Cancel button).
    A plain sleep would keep a cancelled run alive for the whole flood wait - minutes to an hour - before it could notice.
    """
    deadline = time.monotonic() + seconds
    while not _cancelled:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


async def _pause_between_chats() -> None:
    """A random pause (CHAT_PAUSE_SECONDS) before the next chat is fetched - see "Pacing" in the module docstring."""
    await _sleep_unless_cancelled(random.uniform(*CHAT_PAUSE_SECONDS))


async def _wait_out_flood(error: FloodWaitError, what: str, attempt: int, status: dict) -> None:
    """
    Sit through a FloodWaitError that was too long for Telethon to handle itself, then return so the caller retries.

    Raises _FloodWaitStop when the wait exceeds MAX_FLOOD_WAIT_SECONDS (nothing is slept).
    Whether to retry at all is the caller's decision (MAX_FLOOD_RETRIES).
    While waiting, the status file carries flood_wait_seconds so the web UI can say "waiting for Telegram" instead of looking hung; it is removed afterwards.
    Logged as a warning, not an error, and without a traceback: Telegram asking us to slow down is expected on large histories.
    """
    if error.seconds > MAX_FLOOD_WAIT_SECONDS:
        raise _FloodWaitStop(error.seconds) from error
    logger.warning(
        "Rate limited by Telegram while trying to %s (attempt %d/%d) - waiting %ds before retrying. This is normal for large histories, not an error.",
        what, attempt, MAX_FLOOD_RETRIES, error.seconds,
    )
    status["flood_wait_seconds"] = error.seconds
    _write_status(status)
    try:
        await _sleep_unless_cancelled(error.seconds + 1)
    finally:
        status.pop("flood_wait_seconds", None)
        _write_status(status)


async def _with_flood_retry(what: str, call, status: dict):
    """
    Await call() - a zero-argument function returning a coroutine - retrying it through flood waits (see _wait_out_flood()).
    Used for the requests made outside the per-chat loop, which has its own retry handling.
    Raises _FloodWaitStop when a wait is too long or MAX_FLOOD_RETRIES is used up: without the chat list there is nothing to continue with.
    """
    for attempt in range(1, MAX_FLOOD_RETRIES + 1):
        try:
            return await call()
        except FloodWaitError as e:
            if attempt == MAX_FLOOD_RETRIES:
                raise _FloodWaitStop(e.seconds) from e
            await _wait_out_flood(e, what, attempt, status)
    raise AssertionError("unreachable: the last attempt either returns or raises")  # for the type checker


def _refuse_if_conflicting(user_id: int) -> None:
    """
    Exit immediately if this account already has a backfill running, or if its live archiver (main.py) currently holds the Telegram session.

    api/routes/backfill.py's start_backfill() already refuses both cases
    - but only when a backfill is launched THROUGH the API (the web UI's "Start Backfill" button).
    Running `python backfill.py` directly from a terminal skipped that check entirely
    - the same gap main.py itself had on the archiver side (see main.py's own _refuse_if_already_running()).
    Checking here, at the one true entry point regardless of how it was launched, is what actually closes it.
    Only THIS account's files are consulted - other accounts have their own sessions and are unaffected.
    Runs before anything (DB, Telethon, the backfill_runs row) is opened, so exiting here needs no cleanup.
    """
    if is_backfill_running(backfill_status_path(user_id)):
        logger.error(
            "A backfill is already running for this account. "
            "Refusing to start a second one - they would both write to the same database and fight over the same Telegram session."
        )
        sys.exit(1)

    if is_archiver_running(heartbeat_path(user_id)):
        logger.error(
            "The live userbot (main.py) appears to be connected for this account. "
            "Stop it before starting a backfill - Telethon sessions only support one active connection at a time."
        )
        sys.exit(1)


async def _find_chat(client, target: int | str) -> Any:
    """
    The Telethon entity for `--chat`.

    A numeric id is first looked for in the account's own chat list:
    a fresh process has an empty entity cache, and asking Telegram for a group or channel by its bare id needs the access hash it does not have yet
    (the chat list brings it along).
    The web UI's chat picker passes exactly these ids.
    Whatever is not in the chat list (a chat the account has left, a @username, an invite link) falls back to get_entity(), as before.
    """
    if isinstance(target, int):
        async for dialog in client.iter_dialogs():
            if dialog.id == target:
                return dialog.entity
    return await client.get_entity(target)


def _format_wait(seconds: int) -> str:
    """'3 h 05 min' / '12 min' / '40 s' - for the log line only."""
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min" if minutes else f"{seconds} s"


def _refuse_if_cooling_down(conn) -> None:
    """
    Exit when the account's last COMPLETED backfill ended less than BACKFILL_COOLDOWN_HOURS ago ("Cooldown" in the module docstring).

    Called once the archive is reachable (the rule lives in its backfill_runs table) and before this run writes its own row.
    The caller closes the database on the way out.
    """
    remaining = db.queries.backfill_cooldown_remaining(conn, settings.backfill_cooldown_seconds)
    if remaining > 0:
        logger.error(
            "The last backfill finished less than %d hours ago. Another one may start in %s "
            "(BACKFILL_COOLDOWN_HOURS in .env; a cancelled or flood-stopped run does not count). Nothing was fetched.",
            settings.backfill_cooldown_seconds // 3600, _format_wait(remaining),
        )
        conn.close()
        db.close_db()
        sys.exit(1)


def _refuse_unsupported_mode(conn, mode: str) -> None:
    """
    "text" and "media" need the file-type migration (e2b7c4d9a613: the 'skipped' status and the file marker); on an archive without it they stop before touching anything.
    "all" always works - it is the behaviour every archive had before modes existed.
    """
    if mode not in MODES:
        logger.error("Unknown mode %r; choose one of: %s.", mode, ", ".join(MODES))
    elif mode == "all" or db.queries.media_policy_ready(conn):
        return
    else:
        logger.error(
            "Mode %r needs a newer archive database. Run `python scripts/migrate_archives.py` (see RUNBOOK.md, 'Updating existing archives'). Nothing was fetched.",
            mode,
        )
    conn.close()
    db.close_db()
    sys.exit(1)


def _handle_sigterm(signum, frame) -> None:
    """
    Flip a flag rather than raising immediately - lets the current message finish and insert_message() own commit land cleanly before the run loop notices and exits,
    so a Cancel click doesn't look like a crash in backfill_runs.
    """
    global _cancelled
    _cancelled = True
    logger.info(
        "Cancellation requested - finishing the current message, then stopping."
    )


# What a backfill looks at (--mode / the Backfill tab's "What to fetch"):
#   all   - messages and their files (the default);
#   text  - message text only: files are recorded as 'skipped' (the chat view still says "Photo") and a message with no text at all is not stored;
#   media - only messages that carry a file: a message without one is passed over, the files go to the download queue.
MODES = ("all", "text", "media")


def _resume_min_id(conn, chat_id: int, mode: str) -> int | None:
    """
    Where this mode's walk of a chat may resume from (None = the whole chat).

    Each kind of walk has its own marker: text -> chats.backfill_covered_id, media -> chats.media_walked_id.
    "all" does both jobs, so it can only skip what BOTH markers have covered: the lower of the two, and a full walk while either is unset
    (after the file migration every chat starts unset, so the first "all" run re-checks old messages for files - exactly what that run is for).
    """
    covered = db.queries.get_backfill_covered_id(conn, chat_id) if mode != "media" else None
    walked = db.queries.get_media_walked_id(conn, chat_id) if mode != "text" else None
    if mode == "text":
        return covered
    if mode == "media":
        return walked
    return None if covered is None or walked is None else min(covered, walked)


def _record_markers(conn, chat_id: int, last_seen_id: int, mode: str) -> None:
    """Advance the marker(s) this mode is entitled to (see _resume_min_id).
    Does not commit - the caller batches it with the rows it covers."""
    if mode != "media":
        db.queries.record_backfill_covered_id(conn, chat_id, last_seen_id)
    if mode != "text":
        db.queries.record_media_walked_id(conn, chat_id, last_seen_id)


async def backfill_chat(
    client: TelegramClient, conn, chat, limit: int | None, min_id: int | None = None, mode: str = "all"
) -> tuple[int, int]:
    """
    Archive a chat's history - either all of it, or (the default, see backfill.py's module docstring) only messages newer than min_id.
    `mode` says what to fetch: "all", "text" or "media" (see MODES).

    Returns (stored, skipped).
    skipped covers both "already archived" (INSERT OR IGNORE no-op) and "nothing worth archiving" (media, stickers, unsupported service messages)
    - not distinguished further, since this is just a progress summary, not an audit log.
    """
    chat_type = get_chat_type(chat)
    # `or None` at the end matters: Telegram sends an EMPTY first name for a deleted account, and "" (unlike None) is a real value to upsert_chat(),
    # which would overwrite the name the archive already knows.
    # None means "could not resolve the name" and changes nothing.
    chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", None) or None
    chat_username = (getattr(chat, "username", None) or "").lstrip("@") or None

    # Detect a completed group->supergroup migration before archiving history, so any messages stored below go under the canonical (new) chat_id,
    # and any pre-existing basic-group rows get picked up by merge_migrated_chats.py instead of staying orphaned.
    if chat_type in ("supergroup", "channel"):
        try:
            full = await client(functions.channels.GetFullChannelRequest(chat))
            # client(request) is typed as `result | list[result]` (the list form is for batched requests, which this is not),
            # so reach the attribute defensively instead of via `full.full_chat`.
            migrated_from = getattr(getattr(full, "full_chat", None), "migrated_from_chat_id", None)
            if migrated_from:
                old_chat_id = utils.get_peer_id(PeerChat(migrated_from))
                db.queries.record_chat_migration(
                    conn, old_chat_id=old_chat_id, new_chat_id=chat.id
                )
        except FloodWaitError:
            # Not a failed probe: Telegram asked us to wait.
            # Let run()'s retry handling sit it out and try this chat again,
            # instead of logging a traceback and carrying on without the check (which would also hammer Telegram while it is asking us to stop).
            raise
        except Exception:
            logger.exception(
                f"Could not check migration status for '{chat_name}' - continuing without it."
            )

    db.queries.upsert_chat(
        conn,
        chat_id=utils.get_peer_id(chat),
        name=chat_name,
        chat_type=chat_type,
        username=chat_username,
    )

    # The message ignore rules that apply to this chat, loaded once (they cannot change in the middle of a chat in any way that matters).
    # A failure here ends this chat as "failed" in run()'s per-chat handler - never archived on a guess; an archive without the tables has no rules.
    ignore_patterns = db.queries.get_ignore_patterns(conn, utils.get_peer_id(chat))

    # The retention policy's cutoff (None = no active policy): messages older than it would be deleted again by the archiver's next run,
    # so importing them is wasted work and would briefly bring back what the owner chose to forget.
    retention_cutoff = db.queries.active_retention_cutoff(conn)
    if retention_cutoff is not None:
        logger.info("  Automatic cleanup is ON: messages older than %s are not imported.", retention_cutoff.date().isoformat())

    stored = 0
    skipped = 0
    # Why messages were skipped, for the end-of-chat log line: a bare "skipped: 111" cannot tell "already archived" from "all expired".
    why: Counter[str] = Counter()
    processed = 0
    # The id of the last message this walk got past (stored or deliberately skipped) - what chats.backfill_covered_id records.
    # Messages are walked oldest -> newest, so everything at or below it has been dealt with.
    last_seen_id = 0

    # reverse=True walks oldest -> newest.
    # Doesn't affect correctness here (INSERT OR IGNORE makes this safe to interrupt and resume any time),
    # just makes archived_at ordering read naturally if you ever look at the raw table.
    # min_id=0 (Telethon's own default) means "no lower bound" - a brand-new chat with nothing archived yet passes min_id=None here, which becomes 0.
    async for message in client.iter_messages(
        chat,
        reverse=True,
        # Telethon documents limit=None as "no cap" but annotates the parameter `float`, hence the targeted ignore.
        limit=limit,  # pyright: ignore[reportArgumentType]
        min_id=min_id or 0,
        # A pause between pages of 100 - see "Pacing" in the module docstring.
        wait_time=PAGE_WAIT_SECONDS,
    ):
        if _cancelled:
            logger.info(f"Cancelled mid-chat at '{chat_name}' ({processed} processed).")
            break

        processed += 1
        last_seen_id = message.id
        if processed % PROGRESS_INTERVAL == 0:
            logger.info(
                f"  ...{processed} messages processed so far ({stored} stored)."
            )

        # A message that is set to self-destruct (chat auto-delete timer, view-once media) is never archived - same rule as the live handlers.
        # A timer message that is still around when history is fetched is simply not yet expired; keeping it would keep it from disappearing.
        if is_self_destructing(message):
            skipped += 1
            why['a self-destruct timer (auto-delete chat or view-once media)'] += 1
            continue

        # Same rule as the live handlers (handlers/ingest_filter.py): a plain-text message whose text matches an ignore rule is not archived.
        # Counted as skipped. Nothing about the message or the rule is logged.
        if matches_any(ignore_patterns, matchable_text(message)):
            skipped += 1
            why['an ignore rule'] += 1
            continue

        if retention_cutoff is not None and message.date < retention_cutoff:
            skipped += 1
            why['older than the retention cutoff (Settings -> automatic cleanup)'] += 1
            continue

        text = resolve_message_text(message)
        kind = media_kind(message)
        # Neither text nor a file we store (sticker, GIF, service message): nothing to archive.
        # A media-only message (a photo without caption) is stored with text NULL and a message_media row, like the live handler does.
        if not text and kind is None:
            skipped += 1
            why['nothing to store (sticker, GIF, service message...)'] += 1
            continue
        # The mode narrows that: "media" wants only messages with a file, "text" only messages with text (a captionless photo has none).
        if (mode == "media" and kind is None) or (mode == "text" and not text):
            skipped += 1
            why['outside the chosen mode'] += 1
            continue

        if message.sender_id is not None:
            sender = await message.get_sender()
            username, first_name, last_name = get_sender_fields(sender)
            # commit=False:
            # grouped with insert_message()'s own commit below
            # (and that one is itself batched every PROGRESS_INTERVAL rows, not per-message - see the conn.commit() calls further down).
            # Keeps the sender upsert and the message insert atomic as one unit rather than two separately-committed statements,
            # and avoids paying a Postgres WAL fsync for every single row - see insert_message()'s docstring in db/queries.py.
            db.queries.upsert_sender(
                conn,
                sender_id=message.sender_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                commit=False,
            )

        row_id = db.queries.insert_message(
            conn,
            tg_message_id=message.id,
            chat_id=utils.get_peer_id(chat),
            sender_id=message.sender_id,
            text=text or None,
            date=message.date,
            media_kind=kind,
            media_size=media_size(message) if kind else None,
            # "text only" records the file without queueing it; the owner's file-type choice is applied inside insert_message().
            media_skip=mode == "text",
            # Batched below, not per-row - see insert_message()'s docstring.
            commit=False,
        )
        if row_id is not None:
            stored += 1
        else:
            skipped += 1
            why['already archived'] += 1
            # Already archived.
            # If it carries a file the archive never recorded (history from before media archiving), record it now - see "Files of already archived messages".
            if kind and mode != "text":
                db.queries.add_missing_media_row(
                    conn,
                    tg_message_id=message.id,
                    chat_id=utils.get_peer_id(chat),
                    kind=kind,
                    size_bytes=media_size(message),
                    commit=False,
                )

        if processed % PROGRESS_INTERVAL == 0:
            # The marker goes in the same transaction as the rows it covers (it does not commit by itself), so it can never claim more than was stored.
            if last_seen_id:
                _record_markers(conn, utils.get_peer_id(chat), last_seen_id, mode)
            conn.commit()

    # Also reached after a cancel: whatever was handled before the stop counts as covered, so the next run resumes from there.
    if last_seen_id:
        _record_markers(conn, utils.get_peer_id(chat), last_seen_id, mode)
    conn.commit()  # flush whatever's left in the final partial batch
    if skipped:
        # Counts only, never text or names (the logging rules).
        logger.info("  skipped %d: %s.", skipped, "; ".join(f"{n} {reason}" for reason, n in why.most_common()))
    return stored, skipped


async def run(
    user_id: int, chat_selector: str | None, limit: int | None, force_full: bool = False, mode: str = "all"
) -> None:
    try:
        account = load_account_context(user_id)
    except AccountContextError as exc:
        logger.error("Cannot start: %s", exc)
        sys.exit(1)
    except Exception:
        logger.exception("Cannot start: the control database could not be read - is Docker (Postgres) running?")
        sys.exit(1)

    # Schema application is `alembic upgrade head`,
    # run when the archive was provisioned (db/provisioning.py) - not called here (see db/schema.py's module docstring for why).
    # This assumes migrations have already been applied.
    db.init_server(settings.control_database_url)
    db.init_db(db.server_database_url(account.archive_db_name, account.archive_login).render_as_string(hide_password=False))

    # init_db() only builds the Engine - it doesn't open a connection (see db/connection.py's docstring).
    # Without this check, an unreachable Postgres (e.g. Docker not running) would only surface once the loop
    # below actually tried to insert something - same silent-hang failure mode main.py had.
    try:
        db.check_connection()
    except Exception:
        logger.error(
            "Cannot reach the archive database %r - is Docker (Postgres) running? "
            "Check `docker compose ps`.",
            account.archive_db_name,
        )
        db.close_db()
        sys.exit(1)

    conn = db.get_connection()
    _refuse_unsupported_mode(conn, mode)
    _refuse_if_cooling_down(conn)

    # A second, separate pooled connection dedicated to backfill_runs bookkeeping (the status row's start/finish, not the archived data itself)
    # - kept apart from `conn` above so the run-status update at the very end
    # (after the whole backfill loop) isn't entangled in whatever transaction state `conn` is in at that point.
    # Same separation of concerns the original SQLite version had (a dedicated sqlite3.connect() for this one purpose),
    # just now a second pooled connection rather than a second raw one.
    run_conn = db.get_connection()
    run_id = run_conn.execute(
        sql_text(
            "INSERT INTO backfill_runs (status, chat_selector) VALUES ('running', :chat_selector) RETURNING id"
        ),
        {"chat_selector": chat_selector},
    ).scalar()
    run_conn.commit()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    status = {
        "state": "running",
        "pid": os.getpid(),
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "chats_total": None,
        "chats_done": 0,
        "current_chat": None,
    }
    _write_status(status)

    # Non-interactive on purpose - see main.py's "Telethon client" step for why connect()+is_user_authorized() replaces client.start(phone=...),
    # and why the client is built inside the try.
    client: TelegramClient | None = None
    try:
        client = TelegramClient(StringSession(account.session_string), account.api_id, account.api_hash, **client_options())
        await client.connect()
        authorized = await client.is_user_authorized()
    except Exception:
        logger.exception("Could not connect to Telegram with this account's saved credentials.")
        authorized = False

    if not authorized or client is None:
        logger.error(
            "This account's Telegram session is not valid (revoked, expired, corrupted, or the API credentials were rejected). "
            "Sign in to Telegram again in the web UI (Settings -> Telegram setup)."
        )
        # Leave an honest record instead of a "running" row/status that nothing will ever finish.
        status["state"] = "error"
        _write_status(status)
        run_conn.execute(
            sql_text("UPDATE backfill_runs SET finished_at = CURRENT_TIMESTAMP, status = 'error' WHERE id = :run_id"),
            {"run_id": run_id},
        )
        run_conn.commit()
        run_conn.close()
        conn.close()
        if client is not None:
            await disconnect_client(client)
        db.close_db()
        sys.exit(1)

    me = await get_authenticated_user(client)
    logger.info(f"Authenticated as: {me.first_name} (id={me.id})")

    # list[Any]: Telethon's get_entity()/iter_dialogs() stubs are unions of entity classes (and even lists),
    # but every element we keep here is a single User/Chat/Channel entity - the attributes used below (title, first_name, username, id) exist on all of them.
    chats: list[Any] = []
    # Set when Telegram asked for a wait this run will not sit through (see _FloodWaitStop): the run then ends with state "error".
    flood_stopped = False
    flood_wait_seconds = 0
    try:
        if chat_selector:
            # Numeric chat_id or @username/invite-style identifier - get_entity() resolves either.
            try:
                target = int(chat_selector)
            except ValueError:
                target = chat_selector
            entity = await _with_flood_retry(f"look up chat {chat_selector!r}", lambda: _find_chat(client, target), status)
            # get_entity() is typed as possibly returning a list (it does for list input); we pass one identifier, so it is one entity.
            chats = [entity[0] if isinstance(entity, list) else entity]
        else:
            logger.warning(
                "No --chat given - backfilling EVERY chat you're in."
                "This can take a long time and make many API requests for accounts with "
                "years of history or large channels. Press Ctrl-C to abort."
            )

            async def list_dialogs() -> list[Any]:
                return [dialog.entity async for dialog in client.iter_dialogs()]

            chats = await _with_flood_retry("list your chats", list_dialogs, status)
    except _FloodWaitStop as stop:
        flood_stopped = True
        logger.error(
            "Telegram kept asking to slow down (last wait: %ds) while the chats were being looked up - stopping instead of waiting any longer. Try again later.",
            stop.seconds,
        )

    total_stored = 0
    total_skipped = 0
    total_blocked = 0
    any_failures = False
    # True when some chat was given up on after MAX_FLOOD_RETRIES flood waits (see the for-else in the chat loop).
    flood_gave_up = False
    # True once a chat was really requested from Telegram: the next chat then waits a random moment first (see "Pacing").
    # Blocked chats never set it - they cost Telegram nothing, so they cost no pause.
    fetched_since_pause = False

    status["chats_total"] = len(chats)
    _write_status(status)

    for chat in chats:
        if fetched_since_pause:
            await _pause_between_chats()
            fetched_since_pause = False
        # Checked per chat, not only per message: after a Cancel the remaining chats must not each send one more request just to notice it
        # (backfill_chat() only looks at the flag once it has its first message).
        if _cancelled:
            break

        name = (
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or str(chat.id)
        )
        logger.info(f"Backfilling '{name}'...")

        stored = skipped = 0
        failed = False
        blocked = False
        for attempt in range(1, MAX_FLOOD_RETRIES + 1):
            try:
                status["current_chat"] = name

                # The ingest blocklist - decided before a single message of this chat is requested, let alone stored (see handlers/ingest_filter.py).
                # Inside the try on purpose:
                # if the rules cannot be read this chat is reported as failed and skipped like any other per-chat error, never archived on a guess.
                # `break` also skips the for-else below, which is only for "ran out of flood-wait retries".
                if db.queries.is_chat_blocked(conn, utils.get_peer_id(chat)):
                    blocked = True
                    break

                # Recomputed on every attempt, not just the first:
                # if an earlier attempt got partway through before a flood wait hit, whatever it already committed has raised this chat's backfill marker
                # - so a retry naturally resumes close to where it stopped instead of re-walking from scratch,
                # with no special resume logic needed beyond recomputing this.
                # (Not with --full: that deliberately ignores the marker, so a retry there starts over.)
                chat_min_id = None if force_full else _resume_min_id(conn, utils.get_peer_id(chat), mode)
                fetched_since_pause = True
                stored, skipped = await backfill_chat(
                    client, conn, chat, limit, min_id=chat_min_id, mode=mode
                )
                break
            except FloodWaitError as e:
                # Expected, not a bug: Telegram's own rate limit.
                # Telethon already auto-waits for short flood waits internally (below its flood_sleep_threshold);
                # this only fires for longer ones.
                # Logged plainly (no traceback - this isn't an error, it's Telegram asking us to slow down) and retried.
                # A retry recomputes chat_min_id above,
                # so it resumes from wherever this chat's archive actually got to rather than re-walking the whole thing again - see that comment.
                # After the last attempt there is nothing to wait for: the for-else below gives up on this chat.
                if attempt < MAX_FLOOD_RETRIES:
                    try:
                        await _wait_out_flood(e, f"backfill '{name}'", attempt, status)
                    except _FloodWaitStop:
                        # Too long to sit through (MAX_FLOOD_WAIT_SECONDS): end the whole run, see below.
                        flood_stopped = True
                        flood_wait_seconds = e.seconds
                        break
            except Exception:
                # One chat failing for a genuinely unexpected reason (permissions, a weird entity type, etc.) shouldn't stop the rest of the run.
                logger.exception(
                    f"Failed to backfill '{name}' - skipping to the next chat."
                )
                failed = True
                break
        else:
            logger.error(
                f"Gave up on '{name}' after {MAX_FLOOD_RETRIES} flood-wait "
                f"retries - Telegram kept rate limiting this chat. Try "
                f"again later, perhaps with --chat '{name}' on its own."
            )
            any_failures = True
            # A chat Telegram kept rate limiting is unfinished work, like a flood stop: the run must not count as completed,
            # or the cooldown would stand between the owner and the retry the message above recommends.
            flood_gave_up = True
            status["chats_done"] += 1
            _write_status(status)
            continue

        if flood_stopped:
            logger.error(
                "Telegram asked for a wait of %ds while backfilling '%s' - stopping the run instead of waiting that long. "
                "What was archived so far is kept; start the backfill again later and it continues from there.",
                flood_wait_seconds, name,
            )
            any_failures = True
            break

        if blocked:
            logger.info(
                f"Skipping '{name}' - it is on your ingest blocklist, so nothing was fetched or stored "
                f"(remove it under Settings -> Blocked chats if you want it archived)."
            )
            total_blocked += 1
            status["chats_done"] += 1
            _write_status(status)
            continue

        if failed:
            any_failures = True
            status["chats_done"] += 1
            _write_status(status)
            continue

        logger.info(f"  '{name}': {stored} stored, {skipped} skipped.")
        total_stored += stored
        total_skipped += skipped
        status["chats_done"] += 1
        _write_status(status)

    logger.info(
        f"Backfill {'stopped early' if flood_stopped else 'complete'}: {total_stored} stored, {total_skipped} skipped overall"
        + (f", {total_blocked} chat(s) skipped because they are blocked." if total_blocked else ".")
    )

    final_status = (
        "cancelled"
        if _cancelled
        else (
            "error"
            if flood_stopped or flood_gave_up or (total_stored == 0 and total_skipped == 0 and any_failures)
            else "completed"
        )
    )
    status["state"] = final_status
    _write_status(status)
    run_conn.execute(
        sql_text(
            "UPDATE backfill_runs SET finished_at = CURRENT_TIMESTAMP, status = :status, "
            "messages_stored = :messages_stored, messages_skipped = :messages_skipped WHERE id = :run_id"
        ),
        {
            "status": final_status,
            "messages_stored": total_stored,
            "messages_skipped": total_skipped,
            "run_id": run_id,
        },
    )
    run_conn.commit()
    run_conn.close()

    await disconnect_client(client)
    conn.close()
    db.close_db()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive historical messages sent before TeleVault was running."
    )
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Control-database id of the account to backfill. Its Telegram login and archive database are read from the control database.",
    )
    parser.add_argument(
        "--chat",
        metavar="ID_OR_USERNAME",
        default=None,
        help="Backfill only this chat (numeric chat ID or @username). Omit to backfill every chat you're in.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of messages fetched per chat. Omit for full history. Useful for a quick test run.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Force a full re-walk of each chat's history, ignoring what's already archived."
            "Off by default: normally only messages newer than the last archived one per chat are fetched,"
            "which is much faster and lighter on Telegram's API quota on repeat runs."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="all",
        help=(
            "What to fetch: 'all' (messages and their files, the default), 'text' (message text only - files are recorded but not downloaded) "
            "or 'media' (only messages that carry a file). 'text' and 'media' need the archive to be migrated (scripts/migrate_archives.py)."
        ),
    )
    args = parser.parse_args()

    setup_logging(log_level=settings.log_level, log_file=settings.log_file)

    global _status_file
    _status_file = backfill_status_path(args.user_id)
    _refuse_if_conflicting(args.user_id)

    try:
        asyncio.run(run(args.user_id, args.chat, args.limit, args.full, args.mode))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()