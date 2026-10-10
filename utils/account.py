"""
Loads ONE account's Telegram credentials and archive location for a worker process (main.py, backfill.py).

Why this exists:
    Telegram credentials used to come from .env (one account per installation).
    TeleVault is now multi-account: each account links its own Telegram through the web UI, and the result - api_id, api_hash and the Telethon session string -
    is stored ENCRYPTED on that account's `users` row in the control database (see api/routes/telegram.py and utils/crypto.py).
    A worker started for account N therefore has to fetch and decrypt those values itself.

How a worker gets its account:
    The API server starts workers as `python main.py --user-id N` / `python backfill.py --user-id N`.
    Only the (non-secret) user id travels on the command line - command lines are visible to every local user through `ps`/Task Manager.
    The session string, which is equivalent to being logged in as that person on Telegram,
    is read from the control database by the worker itself and never leaves its memory.

Requires the same FERNET_KEY the API server used to encrypt the values; a different key makes decryption fail (see utils/crypto.py).
"""

import logging
from dataclasses import dataclass

import control_db
from config import settings
from utils.crypto import decrypt_secret

logger = logging.getLogger(__name__)


class AccountContextError(Exception):
    """
    The account can't be run as a worker right now.
    `reason` is a short machine-readable code (useful in logs and tests); the message is for the operator reading the worker's output.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AccountContext:
    """Everything a worker needs to act as one account.
    Frozen: nothing should mutate it after load."""

    user_id: int
    username: str
    api_id: int
    api_hash: str            # secret - never log
    session_string: str      # secret - never log, never put on a command line
    archive_db_name: str     # the account's own archive database (users.archive_db_ref)


def load_account_context(user_id: int) -> AccountContext:
    """
    Read and decrypt one account's worker settings from the control database.

    Opens and closes its own control-DB pool: a worker only needs these values once at startup,
    so it keeps no control-DB connection open for its (possibly very long) lifetime.

    Raises AccountContextError if the account can't run: it doesn't exist, is locked by an admin,
    hasn't linked Telegram yet, has no archive database yet, or its stored secrets can't be decrypted (wrong FERNET_KEY).
    """
    control_db.init_control_db(settings.control_database_url)
    try:
        with control_db.get_connection() as conn:
            user = control_db.queries.get_user_by_id(conn, user_id)
    finally:
        control_db.close_db()

    if user is None:
        raise AccountContextError(f"No account with id={user_id}.", "no_such_user")
    if user["is_locked"]:
        raise AccountContextError(f"Account '{user['username']}' is locked by an administrator.", "account_locked")
    if not user["telegram_api_id"] or not user["telegram_api_hash"]:
        raise AccountContextError(
            f"Account '{user['username']}' has not saved Telegram API credentials yet (Settings -> Telegram setup in the web UI).",
            "telegram_not_linked",
        )
    if not user["telegram_session_string"]:
        raise AccountContextError(
            f"Account '{user['username']}' has not signed in to Telegram yet (Settings -> Telegram setup in the web UI).",
            "telegram_not_linked",
        )
    if not user["archive_db_ref"]:
        raise AccountContextError(
            f"Account '{user['username']}' has no archive database yet (finish the setup wizard in the web UI).",
            "archive_unattached",
        )

    try:
        api_id = int(decrypt_secret(user["telegram_api_id"]))  # type: ignore[arg-type]
        api_hash = decrypt_secret(user["telegram_api_hash"])
        session_string = decrypt_secret(user["telegram_session_string"])
    except Exception as exc:  # cryptography's InvalidToken, or int() on garbage - both mean "these bytes aren't what we stored"
        raise AccountContextError(
            "Could not decrypt this account's stored Telegram credentials - is FERNET_KEY the same one they were encrypted with?",
            "decrypt_failed",
        ) from exc

    assert api_hash is not None and session_string is not None  # decrypt_secret() only returns None for a None input, ruled out above
    return AccountContext(
        user_id=user["id"],
        username=user["username"],
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        archive_db_name=user["archive_db_ref"],
    )
