"""
Symmetric encryption for the three credential columns on control_db.schema.users (telegram_api_id, telegram_api_hash, telegram_session_string).

Why these three columns specifically need this and nothing else in the app does:
    Everything in db/schema.py (the archive) is content the user chose to have TeleVault store - if that database leaks,
    the damage is "someone can read your archived messages".
    These three columns are different in kind:
    telegram_session_string in particular is a live, usable login to the account's real Telegram -
    a leak of this table is a leak of actual Telegram account access, not just archived data.
    Fernet (symmetric, single application-held key) was the confirmed choice over per-user keys or asymmetric encryption -
    there's exactly one place (this app, run by one admin) that ever needs to decrypt these,
    so the extra complexity either alternative would add has no corresponding benefit here.

Where the key comes from:
    settings.fernet_key (required, see config.py's Settings docstring for why there's no default).
    Loaded once at import time, not lazily - a missing/malformed key should fail loudly the first time anything imports this module
    (which will be early, since auth code needs it constantly), not on the first actual encrypt/decrypt call somewhere deep in a request handler.

What this module deliberately does NOT do:
    - No key rotation support.
      Rotating the key would mean re-encrypting every existing row under the new key before the old one can be discarded - a real migration,
      not a config change - and isn't needed at today's scale (sole admin, ~2-10 trusted users).
      Worth revisiting if that changes.
    - No handling of `cryptography.fernet.InvalidToken` here.
      That exception means either the key is wrong (e.g. FERNET_KEY was rotated/changed without re-encrypting existing rows)
      or the stored ciphertext was corrupted/tampered with - both are situations the CALLER needs to know about explicitly
      (e.g. to refuse a Telegram-linked session rather than silently treating it as absent), not something to paper over with a default value here.
"""

from cryptography.fernet import Fernet

from config import settings

# Built once at import time from settings.fernet_key - see module docstring for why not lazily.
# Raises ValueError immediately (fail loudly at import, not on first use) if FERNET_KEY isn't a valid Fernet key:
# the right length, valid urlsafe-base64 - e.g. a stray FERNET_KEY=changeme left over from a copy-pasted .env.example would be caught here,
# not three requests into production.
_fernet = Fernet(settings.fernet_key.encode())


def encrypt_secret(plaintext: str | None) -> str | None:
    """
    Encrypt a value for storage in one of users' credential columns.

    None passes through as None (not encrypted-empty-string) - all three columns are nullable,
    since a freshly-registered user hasn't gone through the Telegram-link flow yet,
    and "no value yet" should stay distinguishable from "empty string" through a round trip.
    """
    if plaintext is None:
        return None
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str | None) -> str | None:
    """
    Decrypt a value read back from one of users' credential columns.

    None passes through as None - mirrors encrypt_secret() above, for the same reason.

    Raises cryptography.fernet.InvalidToken if `ciphertext` wasn't produced by encrypt_secret() under the current FERNET_KEY
    (wrong key, or corrupted/tampered data) - deliberately left uncaught,
    see this module's docstring for why that's the caller's decision to handle, not this function's.
    """
    if ciphertext is None:
        return None
    return _fernet.decrypt(ciphertext.encode()).decode()