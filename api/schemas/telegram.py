"""
Request/response models for api/routes/telegram.py.

Three steps, three request bodies (see that module's own docstring for the full flow):
  1. TelegramCredentialsIn  -> POST /telegram/credentials  (api_id/api_hash from my.telegram.org)
  2. TelegramSendCodeIn     -> POST /telegram/link/send-code  (the phone number to verify)
  3. TelegramConfirmIn      -> POST /telegram/link/confirm    (the code, and later the 2FA password if asked for)
"""

from pydantic import BaseModel, Field


class TelegramCredentialsIn(BaseModel):
    """
    Body for POST /telegram/credentials.

    api_id is accepted as a string even though Telegram's API wants an int, because it arrives from an HTML form field same as everything else here -
    the route converts and validates it's actually numeric before doing anything with it.
    Neither field is validated further than "non-empty" here;
    Telegram itself is the real validator (an invalid pair fails on the next step, send-code, as ApiIdInvalidError -
    see that route's own docstring for why this schema doesn't try to guess Telegram's own validation rules).
    """

    api_id: str = Field(..., min_length=1, description="From my.telegram.org - a numeric string.")
    api_hash: str = Field(..., min_length=1, description="From my.telegram.org, alongside api_id.")


class TelegramCredentialsOut(BaseModel):
    """Returned by POST /telegram/credentials on success."""

    saved: bool = True


class TelegramSendCodeIn(BaseModel):
    """
    Body for POST /telegram/link/send-code.

    phone is intentionally not persisted anywhere (see Consolidated_Decisions_Log.txt: "the phone is not obligatory to store anywhere, just use for verification") -
    it lives only in the in-memory pending-link state this route keeps for the few minutes the handshake takes, then is discarded either way (success or expiry).
    See api/routes/telegram.py's module docstring for that lifetime.
    """

    phone: str = Field(..., min_length=1, description="Including country code, e.g. +15551234567.")


class TelegramSendCodeOut(BaseModel):
    """Returned by POST /telegram/link/send-code on success."""

    sent: bool = True


class TelegramConfirmIn(BaseModel):
    """
    Body for POST /telegram/link/confirm - called up to twice per linking attempt.

    First call: code set, password omitted.
    If the account has no 2FA password, this alone finishes the link.
    If it does, the response comes back with needs_password=True and code is NOT reusable (Telegram already consumed it) - the second call sends password only, code omitted;
    the route tells the two calls apart by which fields are present,
    not by a separate "step" field, and by what state the pending link is already in server-side (see that route's own docstring).
    """

    code: str | None = Field(None, description="The login code Telegram sent, for the first call.")
    password: str | None = Field(None, description="The account's 2FA password, only for the second call if needs_password came back True.")


class TelegramConfirmOut(BaseModel):
    """
    Returned by POST /telegram/link/confirm.

    linked=True means the session string is saved and this account is fully linked.
    needs_password=True (linked still False) means: resubmit with password set instead of code.
    """

    linked: bool
    needs_password: bool = False


class TelegramUnlinkOut(BaseModel):
    """Returned by DELETE /telegram/session on success."""

    unlinked: bool = True


class TelegramStatusOut(BaseModel):
    """
    Returned by GET /telegram/status.

    Read-only summary of how far an account has gotten through the linking flow,
    WITHOUT exposing the encrypted values themselves
    (contrast with UserOut in api/schemas/auth.py, which excludes the telegram_* columns entirely on the same "don't hand out more than the caller needs" logic -
    this endpoint exists specifically so a caller CAN ask "has this been done already?" without that meaning "and also send me the credentials/session").

    Added so the Settings page can open directly to the right step of the wizard instead of always starting a returning,
    already-linked user back at "enter your API credentials" - see web/js/views/settings.js's module docstring for the UX gap this closes.
    """

    has_credentials: bool
    has_session: bool