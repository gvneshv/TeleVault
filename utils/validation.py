"""
Input limits and format rules shared by the API schemas and the admin CLI.

Why this module exists
----------------------
Every value a client can send must have an upper bound.
An unbounded field is a cheap denial-of-service lever
(a 5000-character password is hashed by argon2 at full cost; a multi-megabyte "username" is stored, indexed and logged),
and before this module the password field was the clearest example: passlib refuses anything over 4096 characters by raising,
which surfaced as an unhandled HTTP 500 instead of a client error.

Keeping the numbers in one place means the API (api/schemas/*), the CLI (scripts/manage_admin.py), the HTML `maxlength` attributes in web/ and the tests all agree.
The HTML attributes cannot import Python, so web/ repeats the numbers - if you change one here, grep the web/ folder for the old value.

Registration vs login
---------------------
*Format* rules (the username pattern, the minimum password length) apply only where an account is CREATED.
Login only caps the length: tightening a format rule later must never lock out someone who registered under the old, looser one.

Changing a rule
---------------
Adding a new pattern rule is a one-line change to USERNAME_PATTERN (or a new constant here).
Remember that existing accounts are not re-validated, which is intended.
"""

import re

# --- Accounts ---------------------------------------------------------------------------------------------------------

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
# ASCII letters, digits, dot, underscore, hyphen.
# Deliberately ASCII-only: Unicode usernames invite look-alike (homoglyph) impersonation and normalisation headaches, and nothing in TeleVault needs them yet.
USERNAME_PATTERN = r"^[A-Za-z0-9._-]+$"
_USERNAME_RE = re.compile(USERNAME_PATTERN)

PASSWORD_MIN_LENGTH = 8
# 128 is generous (a 20-word passphrase fits) while keeping argon2's work per request bounded.
# Counted in characters, as Pydantic counts them; the byte length can be up to 4x that for non-ASCII text, still far below passlib's 4096 limit.
PASSWORD_MAX_LENGTH = 128

INVITE_TOKEN_MAX_LENGTH = 128  # tokens we generate are far shorter; this only bounds what an attacker can make us look up

# --- Telegram linking (values come from my.telegram.org / Telegram itself; Telegram stays the real validator) ---------

TELEGRAM_API_ID_MAX_LENGTH = 20  # a numeric string; real ids are ~7-10 digits
TELEGRAM_API_HASH_MAX_LENGTH = 64  # real hashes are 32 hex characters
TELEGRAM_PHONE_MAX_LENGTH = 32  # E.164 allows at most 15 digits plus "+", spaces and dashes
TELEGRAM_CODE_MAX_LENGTH = 16  # login codes are 5-6 digits
TELEGRAM_2FA_PASSWORD_MAX_LENGTH = 256  # Telegram's own cloud-password limit is not documented to us; this is a sanity bound
TELEGRAM_DEVICE_HASH_MAX_LENGTH = 21  # Telegram's session id is an int64 (up to 19 digits plus a sign); web/js/views/settings.js sends it back verbatim
TELEGRAM_DEVICE_HASH_PATTERN = r"^-?[0-9]{1,20}$"  # digits only: it is converted with int() and sent to Telegram, so nothing else may reach that far

# --- Ingest blocklist (api/routes/ingest.py) ---------------------------------------------------------------------------

# Telegram chat ids are 64-bit integers (marked ids such as -1001234567890 are well inside that); the archive stores them as BIGINT.
# Anything outside this range is not a chat id and would only make Postgres raise a numeric-overflow error, which must be a 422, not a 500.
TELEGRAM_CHAT_ID_MIN = -(2**63)
TELEGRAM_CHAT_ID_MAX = 2**63 - 1
# How many chats one account may block.
# A rule can only be created for a chat the archive already knows, so this is far above any real use
# (a very large account is in a few thousand chats at most); it exists so that "no upper bound" is never true of a write endpoint.
INGEST_BLOCKLIST_MAX_COUNT = 2000

# --- Chat view (api/routes/chats.py) ----------------------------------------------------------------------------------

# How many messages one window of a conversation may hold (GET /api/chats/{chat_id}/window).
# Same ceiling as the page size of the older list endpoints: a window is rendered into the DOM in one go, so it must stay small enough for a phone.
CHAT_WINDOW_DEFAULT = 50
CHAT_WINDOW_MAX = 200
# Internal message ids (messages.id, a BIGINT identity column starting at 1) used as cursors and anchors.
# Bounded for the same reason as the chat ids above: a value outside the column's range would make Postgres raise an overflow error, and that must be a 422.
MESSAGE_ID_MIN = 1
MESSAGE_ID_MAX = 2**63 - 1

# --- Search ------------------------------------------------------------------------------------------------------------

SEARCH_QUERY_MAX_LENGTH = 200  # message-text substring search; the pg_trgm scan cost grows with the pattern
CHAT_IDS_MAX_COUNT = 500  # how many chats one request may filter on
DATE_PARAM_MAX_LENGTH = 64  # an ISO 8601 timestamp with offset is ~32 characters


def username_problem(username: str) -> str | None:
    """
    Return a human-readable reason `username` cannot be used for a new account, or None if it is fine.

    Used by the CLI, which has no Pydantic layer; the API gets the same rules declaratively through Field(...) in api/schemas/auth.py.
    """
    if not (USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH):
        return f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters long."
    if not _USERNAME_RE.fullmatch(username):
        return "Username may only contain letters, digits, '.', '_' and '-'."
    return None