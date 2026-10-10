"""
Per-account live archiver - start with: python main.py --user-id N

One of these processes runs per account that wants real-time archiving; the API server starts and stops them
(POST /api/telethon/start|stop, see api/routes/telethon.py), so normally you never run this by hand.
Running it from a terminal works too and is handy for debugging - the same guards apply.

Startup sequence:
  1. Logging
  2. Guard: refuse to start if THIS account's archiver is already running, or if a backfill for it is currently in progress
     (enforced here so it applies no matter how this script is launched - terminal, cron, or the web UI)
  3. Load the account: its Telegram api_id/api_hash/session string (decrypted) and archive database name, from the control database (utils/account.py)
  4. Database (open the connection pool to THIS account's archive - assumes migrations are already applied;
     archive databases are created and migrated by POST /api/archive/provision, not from here)
  5. Telethon client: connect with the saved session. Never interactive - if the session is no longer valid
     (e.g. revoked from Telegram's "Devices" list) this exits with a clear message and the person signs in again in the web UI
  6. Register event handlers
  7. Run until interrupted (Ctrl-C or SIGTERM)
  8. Graceful shutdown

Telethon uses asyncio internally, so the entry point is an async function run via asyncio.run().
Everything Telegram-related happens inside that loop.
"""

import argparse
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
from telethon.sessions import StringSession

import db
from config import settings
from utils.logging_setup import setup_logging
from handlers import on_message, on_delete, on_edit
from api.process_utils import is_archiver_running, is_backfill_running
from utils.account import AccountContextError, load_account_context
from utils.worker_paths import backfill_status_path, heartbeat_path

logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_SECONDS = 20


def _refuse_if_already_running(user_id: int) -> None:
    """
    Exit immediately if this account already has a live archiver or an active backfill holding its Telegram session.

    api/routes/telethon.py's start_archiver() already refuses to start a second archiver,
    or to start one while a backfill is running - but that check only runs when the archiver is launched THROUGH the API (i.e. the web UI's Archiver button).
    Running `python main.py` directly from a terminal bypassed it entirely,
    so nothing stopped two live Telethon sessions - or a live session plus an in-progress backfill - from fighting over the same account.

    Checking here, at the one true entry point regardless of how it was launched, is what actually closes that gap.
    Only THIS account's files are consulted (utils/worker_paths.py): other accounts' workers use different Telegram sessions and are none of its business.
    Runs before anything else (DB, Telethon) is opened, so exiting here needs no cleanup.
    """
    if is_archiver_running(heartbeat_path(user_id)):
        logger.error(
            "TeleVault is already running for this account (heartbeat is live). Refusing to start a second instance."
        )
        sys.exit(1)

    if is_backfill_running(backfill_status_path(user_id)):
        logger.error(
            "A backfill is currently running for this account. "
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
        path.parent.mkdir(parents=True, exist_ok=True)
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
    for deletion-actor inference - only the account owner has access to their own Saved Messages, so any deletion there is deterministically 'self', not a guess.
    See db/queries.py's flag_deleted() for where this is actually used.
    """
    on_message.register(client)
    on_delete.register(client, self_id)
    on_edit.register(client)
    logger.info("Event handlers registered.")


async def main(user_id: int) -> None:
    # ------------------------------------------------------------------ #
    # 1. Logging
    # ------------------------------------------------------------------ #
    setup_logging(log_level=settings.log_level, log_file=settings.log_file)
    logger.info("Starting TeleVault archiver for account id=%s...", user_id)

    # ------------------------------------------------------------------ #
    # 1.5. Single-instance / backfill-exclusion guard (per account)
    # ------------------------------------------------------------------ #
    _refuse_if_already_running(user_id)

    # ------------------------------------------------------------------ #
    # 2. Account (credentials + archive location, from the control DB)
    # ------------------------------------------------------------------ #
    try:
        account = load_account_context(user_id)
    except AccountContextError as exc:
        logger.error("Cannot start: %s", exc)
        sys.exit(1)
    except Exception:
        logger.exception("Cannot start: the control database could not be read - is Docker (Postgres) running?")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 3. Archive database
    # ------------------------------------------------------------------ #
    # Schema application is `alembic upgrade head`, run when the archive was provisioned (db/provisioning.py) - not a function called here at every startup
    # (see db/schema.py's module docstring for why).
    # This just opens the connection pool to THIS account's archive; it assumes migrations have already been applied.
    db.init_server(settings.control_database_url)
    db.init_db(db.server_database_url(account.archive_db_name).render_as_string(hide_password=False))

    # init_db() only builds the Engine - it doesn't open a connection (see db/connection.py's docstring).
    # Without this check, an unreachable Postgres (e.g. Docker not running) wouldn't surface here at all -
    # the archiver would report itself as running normally and only hang, silently, on the first real message.
    try:
        db.check_connection()
    except Exception:
        logger.error(
            "Cannot reach the archive database %r - is Docker (Postgres) running? Check `docker compose ps`.",
            account.archive_db_name,
        )
        db.close_db()
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 3.5. Telethon client
    # ------------------------------------------------------------------ #
    # The login lives in the account's saved session string (control DB), not in a file next to the code -
    # nothing to prompt for here, and this process usually has no terminal to prompt on anyway
    # (it is normally started by the API server), so an interactive client.start(phone=...) would just hang.
    # connect() + is_user_authorized() is the non-interactive equivalent: either the saved session works, or we stop with a clear message.
    # The client is built INSIDE the try on purpose: StringSession() itself raises on a corrupted saved string,
    # and that deserves the same clean exit below, not a raw traceback.
    client: TelegramClient | None = None
    try:
        client = TelegramClient(StringSession(account.session_string), account.api_id, account.api_hash)
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
        if client is not None:
            await client.disconnect()
        db.close_db()
        sys.exit(1)

    me = await client.get_me()
    logger.info(f"Authenticated as: {me.first_name} (id={me.id})")

    # ------------------------------------------------------------------ #
    # 4. Event handlers
    # ------------------------------------------------------------------ #
    register_handlers(client, self_id=me.id)

    # ------------------------------------------------------------------ #
    # 5. Heartbeat loop
    # ------------------------------------------------------------------ #
    heartbeat_file = heartbeat_path(user_id)
    heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_file))

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
    heartbeat_file.unlink(missing_ok=True)

    await client.disconnect()
    db.close_db()
    logger.info("Goodbye.")


if __name__ == "__main__":
    # Wrap asyncio.run() so that Ctrl-C or a SIGTERM-triggered CancelledError reaching this level exits silently rather than printing a traceback.
    # The actual shutdown logic (disconnect, close_db) is inside main(), which handles both exceptions there.
    parser = argparse.ArgumentParser(description="Run the live TeleVault archiver for one account.")
    parser.add_argument(
        "--user-id", type=int, required=True,
        help="Control-database id of the account to archive (shown in the admin panel). Its Telegram login is read from the control database.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.user_id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass