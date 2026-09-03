"""
Application entry point - start TeleVault with: python main.py
 
Startup sequence:
  1. Logging
  2. Guard: refuse to start if another instance is already running, or if a backfill is currently in progress
     (enforced here so it applies no matter how this script is launched - terminal, cron, or the web UI)
  3. Database (open connection, apply schema)
  4. Telethon client (authenticate if needed, then connect)
  5. Register event handlers
  6. Run until interrupted (Ctrl-C or SIGTERM)
  7. Graceful shutdown
 
Telethon uses asyncio internally, so the entry point is an async function run via asyncio.run().
Everything Telegram-related happens inside that loop.
"""

import asyncio
import logging
import signal
import contextlib
import json
import os
import sys
import time
from pathlib import Path

from telethon import TelegramClient

import db
from config import settings
from utils.logging_setup import setup_logging
from handlers import on_message, on_delete, on_edit
from api.process_utils import is_archiver_running, is_backfill_running


logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_SECONDS = 20


def _refuse_if_already_running() -> None:
    """
    Exit immediately if another live archiver session or an active backfill already holds the Telegram connection.

    api/routes/telethon.py's start_archiver() already refuses to start a second archiver,
    or to start one while a backfill is running - but that check only runs when the archiver is launched THROUGH the API (i.e. the web UI's Archiver button).
    Running `python main.py` directly from a terminal bypassed it entirely,
    so nothing stopped two live Telethon sessions - or a live session plus an in-progress backfill - from fighting over the same account.

    Checking here, at the one true entry point regardless of how it was launched, is what actually closes that gap.
    Runs before anything else (DB, Telethon) is opened, so exiting here needs no cleanup.
    """
    if is_archiver_running(settings.heartbeat_path):
        logger.error(
            "TeleVault is already running (heartbeat is live). Refusing to start a second instance."
        )
        sys.exit(1)

    if is_backfill_running(settings.backfill_status_path):
        logger.error(
            "A backfill is currently running." \
            "Stop it before starting the userbot - Telethon sessions only support one active connection at a time."
        )
        sys.exit(1)


async def _heartbeat_loop(path: Path) -> None:
    """
    Periodically touch a heartbeat file while the live userbot is connected.

    Read by GET /api/telethon/status and the backfill-start check,
    so the web UI and API server - both separate processes from this one - can tell whether the live Telegram session is active,
    without any direct coupling beyond this file.
    """
    try:
        while True:
            path.write_text(json.dumps({"pid": os.getpid(), "updated_at": time.time()}))
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


def register_handlers(client: TelegramClient, self_id: int) -> None:
    """
    Attach all Telethon event handlers to the client.
 
    Each handler module registers itself when imported (via @client.on(...) decorators), but the client reference must be injected first.
    The pattern used in each handler module is:
 
        def register(client): ...  <- called here
        # rather than a bare module-level decorator
 
    This keeps the client out of module-level scope in the handler files and makes unit testing easier - you can call register(mock_client) without needing a real Telethon connection.

    self_id (the archiving account's own Telegram user ID) is passed to on_delete specifically:
    it's needed to recognize Saved Messages (the one chat where chat_id == your own user ID)
    for deletion-actor inference — only the account owner has access to their own Saved Messages, so any deletion there is deterministically 'self', not a guess.
    See db/queries.py's flag_deleted() for where this is actually used.
    """
    on_message.register(client)
    on_delete.register(client, self_id)
    on_edit.register(client)
    logger.info("Event handlers registered.")
  

async def main() -> None:
    # ------------------------------------------------------------------ #
    # 1. Logging                                                          
    # ------------------------------------------------------------------ #
    setup_logging(log_level=settings.log_level, log_file=settings.log_file)
    logger.info("Starting TeleVault...")

    # ------------------------------------------------------------------ #
    # 1.5. Single-instance / backfill-exclusion guard                     
    # ------------------------------------------------------------------ #
    _refuse_if_already_running()

    # ------------------------------------------------------------------ #
    # 2. Database                                                         
    # ------------------------------------------------------------------ #
    conn = db.init_db(settings.db_path)
    db.apply_schema(conn)

    # ------------------------------------------------------------------ #
    # 3. Telethon client                                                  
    # ------------------------------------------------------------------ #
    # The session file persists the login so you only enter the auth code once.
    # After that, Telethon reuses the saved session automatically.
    client = TelegramClient(
        settings.session_name,
        settings.api_id,
        settings.api_hash,
    )

    await client.start(phone=settings.phone)

    me = await client.get_me()
    logger.info(f"Authenticated as: {me.first_name} (id={me.id})")

    # ------------------------------------------------------------------ #
    # 4. Event handlers                                                   
    # ------------------------------------------------------------------ #
    register_handlers(client, self_id=me.id)

    # ------------------------------------------------------------------ #
    # 5. Heartbeat loop                                                   
    # ------------------------------------------------------------------ #
    heartbeat_path = Path(settings.heartbeat_path)
    heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_path))

    # ------------------------------------------------------------------ #
    # 6. Run                                                              
    # ------------------------------------------------------------------ #
    logger.info("TeleVault is running. Press Ctrl-C to stop.")

    # Handle SIGTERM gracefully (sent by systemd or Docker on shutdown) add_signal_handler() is Unix-only - Windows raises NotImplementedError.
    # On Windows, Ctrl-C (SIGINT) via the KeyboardInterrupt except below is the only shutdown path needed during local development anyway.
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.stop())
    except NotImplementedError:
        # Expected on Windows. SIGTERM is a Unix concept; skip silently.
        pass

    try:
        await client.run_until_disconnected()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # KeyboardInterrupt : Ctrl-C on all platforms.
        # CancelledError    : Python 3.14 changed asyncio shutdown - the main task is now cancelled rather than allowed to return cleanly, so CancelledError surfaces here instead.
        pass
    
    # ------------------------------------------------------------------ #
    # 6. Shutdown                                                         
    # ------------------------------------------------------------------ #
    logger.info("Shutting down...")

    heartbeat_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat_task
    heartbeat_path.unlink(missing_ok=True)

    await client.disconnect()
    db.close_db()
    logger.info("Goodbye.")
  

if __name__ == "__main__":
    # Wrap asyncio.run() so that Ctrl-C or a SIGTERM-triggered CancelledError reaching this level exits silently rather than printing a traceback.
    # The actual shutdown logic (disconnect, close_db) is inside main(), which handles both exceptions there.
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass