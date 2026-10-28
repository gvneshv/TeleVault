"""
Turn an account's stored archive login (control_db users.archive_db_user / archive_db_password) into the db.ArchiveLogin the connection layer wants.

Why this is its own small module: the password is stored Fernet-encrypted (utils/crypto.py), like the Telegram credentials,
so something has to decrypt it before a connection can be opened - and that must happen identically in the API (api/dependencies.py, routes/health.py, routes/backfill.py),
the workers (utils/account.py) and the maintenance scripts.
db/ must not know about Fernet and control_db/ must not either (control_db/queries.py's docstring), so the one place that does both lives here.

None means "this archive has no login of its own yet" (it predates per-archive roles): the caller then connects with the server's login, as it always did.
"""

import logging
from collections.abc import Mapping
from typing import Any

import db
from utils.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


class ArchiveLoginError(Exception):
    """The stored login exists but cannot be used (wrong FERNET_KEY, or half-written columns).
    Never carries the ciphertext or a password."""


def archive_login_from_row(row: Mapping[str, Any]) -> db.ArchiveLogin | None:
    """
    The archive's own login from a row holding `archive_db_user` and `archive_db_password` (a users row, or control_db.queries.get_archive_login_row()).

    None when neither column is set (an archive that has not been secured yet).
    Raises ArchiveLoginError when only one is set, or when the password does not decrypt under the current FERNET_KEY:
    connecting with the server's login instead would quietly undo the isolation this exists for, so the caller must treat it as "archive unavailable", not fall back.
    """
    role = row.get("archive_db_user")
    encrypted = row.get("archive_db_password")
    if role is None and encrypted is None:
        return None
    if not role or not encrypted:
        raise ArchiveLoginError("The archive's stored database login is incomplete (only one of user and password is set).")
    try:
        password = decrypt_secret(encrypted)
    except Exception as exc:  # cryptography's InvalidToken: wrong FERNET_KEY or corrupted data - say so without echoing either
        raise ArchiveLoginError("Could not decrypt the archive's stored database password - is FERNET_KEY the one it was encrypted with?") from exc
    assert password is not None  # decrypt_secret() only returns None for a None input, ruled out above
    return db.ArchiveLogin(role=role, password=password)


def encrypt_archive_password(password: str) -> str:
    """The value to store in users.archive_db_password."""
    encrypted = encrypt_secret(password)
    assert encrypted is not None
    return encrypted
