"""
The account's Telegram chat list, kept in a small file so the web UI can offer a CHOOSER of chats for a backfill.

Why a file: the chat list exists only in Telegram.
Asking for it needs a live Telegram session, and an account has exactly one:
the archiver holds it (main.py), and the API server never opens its own (api/routes/backfill.py).
So the archiver, which already asks Telegram for the chat list at start-up (handlers/on_read.py),
writes what it learned to `<data_dir>/users/<user_id>/dialogs.json` (utils/worker_paths.py),
and the API reads that file - the same arrangement as the heartbeat and backfill status files.

Why a snapshot rather than the archive's own `chats` table: a backfill exists mainly to import chats the archive has NEVER seen a message from.
Such a chat is in Telegram's list and not in the archive, so only Telegram's list can offer it.

What is stored: for each chat the id (exactly the id the archive and backfill.py use), Telegram's name for it and its type (private / group / supergroup / channel),
in the order Telegram lists them (most recent activity first).
Nothing else: no messages, no members, no phone numbers.
It is the owner's own chat list, written next to their other runtime files and returned only to them.
Names are never logged.
The blocklist and the owner's own labels are NOT applied here: both change independently, so the API applies them when it answers.

Freshness: written once shortly after the archiver starts, then every REFRESH_SECONDS while it runs, so a chat joined later shows up within hours.
A snapshot is only as new as its `updated_at`; a chat that has no entry yet can still be backfilled with "All chats".
Failure policy: this is decoration for the UI.
Any error is logged (never with names) and swallowed, the previous file stays, and archiving is never affected.

Telegram ToS: `iter_dialogs()` is the same passive request on_read.py and backfill.py make; it does not read any chat.
One more pass every few hours is negligible next to what the archiver already does.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from handlers.helpers import get_chat_type
from utils.atomic_write import atomic_write_json
from utils.validation import DIALOG_CATALOG_MAX_COUNT, DIALOG_CATALOG_NAME_MAX_LENGTH
from utils.worker_paths import dialogs_path

logger = logging.getLogger(__name__)

# How often a running archiver re-reads the chat list.
REFRESH_SECONDS = 6 * 3600
# The first pass waits this long, so it does not hit Telegram together with the read-position sync that makes the same request at start-up.
FIRST_PASS_DELAY_SECONDS = 60

_FORMAT_VERSION = 1


def _clean_chat(item: Any) -> dict[str, Any] | None:
    """One snapshot entry as {chat_id, name, chat_type}, or None when it is not usable (a hand-edited or damaged file must never break the API)."""
    if not isinstance(item, dict):
        return None
    chat_id = item.get("chat_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    name = item.get("name")
    name = name.strip()[:DIALOG_CATALOG_NAME_MAX_LENGTH] if isinstance(name, str) and name.strip() else None
    chat_type = item.get("chat_type")
    return {"chat_id": chat_id, "name": name, "chat_type": chat_type if chat_type in ("private", "group", "supergroup", "channel") else "group"}


def read_catalog(user_id: int) -> dict[str, Any] | None:
    """
    The account's snapshot as {"updated_at": str, "chats": [{chat_id, name, chat_type}, ...]}, or None when there is none yet or it cannot be read.

    None means "the archiver has not published a list" - a fresh install, or an archiver that has not run since this feature arrived.
    It is never an error.
    Entries are re-validated and the list re-capped here: the file is plain JSON on disk, not trusted input.
    """
    try:
        raw = json.loads(dialogs_path(user_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("chats"), list):
        return None
    chats: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw["chats"][:DIALOG_CATALOG_MAX_COUNT]:
        chat = _clean_chat(item)
        if chat is not None and chat["chat_id"] not in seen:
            seen.add(chat["chat_id"])
            chats.append(chat)
    updated_at = raw.get("updated_at")
    return {"updated_at": updated_at if isinstance(updated_at, str) else None, "chats": chats}


def write_catalog(user_id: int, chats: list[dict[str, Any]]) -> Path:
    """
    Replace the account's snapshot with `chats`, atomically: the API (another process) sees the old file or the new one, never half of one.
    Uses the same helper as the other status files (utils/atomic_write.py), including its retry for Windows' "file is open by a reader" error.
    """
    path = dialogs_path(user_id)
    atomic_write_json(path, {"version": _FORMAT_VERSION, "updated_at": datetime.now(UTC).isoformat(), "chats": chats[:DIALOG_CATALOG_MAX_COUNT]})
    return path


async def publish_dialogs(client: Any, user_id: int) -> int | None:
    """
    Read Telegram's chat list and store it as the account's snapshot; returns how many chats were stored, or None when it failed (logged, nothing raised).

    Entries without an integer id are skipped.
    Listing stops at DIALOG_CATALOG_MAX_COUNT.
    """
    try:
        chats: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs():
            chat_id = getattr(dialog, "id", None)
            if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                continue
            chats.append({"chat_id": chat_id, "name": getattr(dialog, "name", None), "chat_type": get_chat_type(getattr(dialog, "entity", None))})
            if len(chats) >= DIALOG_CATALOG_MAX_COUNT:
                break
        cleaned = [c for c in map(_clean_chat, chats) if c is not None]
        await asyncio.to_thread(write_catalog, user_id, cleaned)
        logger.info("Chat list published for the backfill's chat picker (%d chats).", len(cleaned))
        return len(cleaned)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Could not publish the chat list for the backfill's chat picker. The previous list (if any) stays; archiving is not affected.")
        return None


async def dialog_catalog_loop(client: Any, user_id: int) -> None:
    """Publish the chat list shortly after start-up and then every REFRESH_SECONDS, for as long as the archiver runs (cancelled on shutdown)."""
    await asyncio.sleep(FIRST_PASS_DELAY_SECONDS)
    while True:
        await publish_dialogs(client, user_id)
        await asyncio.sleep(REFRESH_SECONDS)
