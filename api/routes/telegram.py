"""
Self-service Telegram linking:
POST /telegram/credentials, POST /telegram/link/send-code, POST /telegram/link/confirm.

This is the flow that was missing for every account except the one bootstrapped by hand into settings.api_id/api_hash/session_name (see main.py) -
every OTHER user gets their own api_id/api_hash (from my.telegram.org) and their own Telegram session, stored encrypted on their own `users` row
(telegram_api_id, telegram_api_hash, telegram_session_string - see utils/crypto.py's module docstring for why those three columns specifically are encrypted).
Gated by Depends(get_current_user) only, NOT require_instance_owner:
every account links its OWN Telegram, this isn't an instance-owner-only action the way /telethon/* and /backfill/* are.

The three-step shape:
    1. POST /telegram/credentials   - api_id + api_hash, encrypted and saved.
       Callable again later to redo/replace credentials (see set_telegram_credentials()'s own docstring for what that clears).
    2. POST /telegram/link/send-code - phone number, NOT persisted anywhere (see TelegramSendCodeIn's own docstring) -
       triggers Telegram's own send_code_request using the credentials saved in step 1.
       Requires step 1 to have already happened for this account.
    3. POST /telegram/link/confirm  - the code Telegram sent (and, only if the account has 2FA enabled, a second call with the account's password instead).
       On success, the resulting Telethon session string is encrypted and saved - the account is now linked.

What this deliberately does NOT do yet (see project roadmap - these are separate, later steps):
    - Does not provision an archive database.
      archive_db_ref still has to be set by an admin via scripts/manage_admin.py's set-archive subcommand afterward, same manual step as today.
      Linking Telegram and having somewhere to store the archived messages are separable concerns,
      and automating the latter (CREATE DATABASE + running alembic programmatically, safely, exactly once) is real, separate work this endpoint isn't trying to also solve.
    - Does not start a live userbot process for the newly-linked account.
      main.py is still the one physical process for the one instance owner;
      per-user worker processes (one per linked account) are the follow-up architecture work this flow is a prerequisite for, not something it does itself.

In-memory pending-link state (see _PENDING_LINKS below):
the phone_code_hash and the not-yet-signed-in TelegramClient/StringSession returned by send_code_request() have to survive between the send-code and confirm HTTP requests,
which are two separate requests with no other shared state.
Kept in a plain module-level dict keyed by user_id rather than persisted to control_db, on the assumption this process runs as a SINGLE uvicorn worker
(the deployment this whole project targets - see project brief: a handful of trusted family members, not a scaled-out multi-worker service).
Restarting the API process mid-handshake loses any in-progress link attempt - that's an acceptable failure mode (the user just calls send-code again)
for something that only matters for the few minutes a linking attempt takes,
and simpler than round-tripping partial Telethon session state through Postgres for a case this rare.
If this API ever runs with more than one worker process, this state needs to move somewhere shared (e.g. a short-lived control_db table) -
it will silently misbehave otherwise (a send-code handled by worker A and the matching confirm landing on worker B would find no pending link at all).
"""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.engine import Connection
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import control_db
from api.dependencies import get_control_db, get_current_user
from api.schemas import (
    TelegramConfirmIn,
    TelegramConfirmOut,
    TelegramCredentialsIn,
    TelegramCredentialsOut,
    TelegramSendCodeIn,
    TelegramSendCodeOut,
)
from utils.crypto import decrypt_secret, encrypt_secret
from utils.security import DecodedAccessToken

router = APIRouter(prefix="/telegram", tags=["telegram"])

# How long a send-code/confirm handshake can sit half-finished before it's treated as abandoned and discarded on next access.
# Matches roughly how long Telegram itself keeps a phone_code_hash valid - there's no point holding this open longer than Telegram would honor it anyway.
PENDING_LINK_TTL_SECONDS = 10 * 60

# A generous-but-real cap on wrong code/password guesses within one handshake, so a stolen access token can't be used to brute-force a 2FA password indefinitely.
# Deliberately NOT persisted anywhere (unlike auth.py's login throttle) - restarting via send-code resets it,
# but doing so re-triggers Telegram's OWN rate limiting on that phone number (FloodWaitError), which is the real backstop here;
# this counter only needs to stop casual retrying within a single still-open handshake.
MAX_CONFIRM_ATTEMPTS = 5


class _PendingLink:
    """One in-progress send-code/confirm handshake for one user_id. See this module's own docstring for why this lives in memory rather than in control_db."""

    __slots__ = ("client", "phone", "phone_code_hash", "awaiting_password", "attempts", "created_at")

    def __init__(self, client: TelegramClient, phone: str, phone_code_hash: str) -> None:
        self.client = client
        self.phone = phone
        self.phone_code_hash = phone_code_hash
        self.awaiting_password = False
        self.attempts = 0
        self.created_at = time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > PENDING_LINK_TTL_SECONDS


# user_id -> _PendingLink. _lock guards inserting/discarding entries (not the Telethon calls themselves)
# so two requests for the same account can't race each other into holding two live clients for one user_id at once.
_pending_links: dict[int, _PendingLink] = {}
_lock = asyncio.Lock()


async def _discard_pending_link(user_id: int) -> None:
    """Disconnect and drop any pending link for user_id, if one exists. Safe to call unconditionally."""
    async with _lock:
        pending = _pending_links.pop(user_id, None)
    if pending is not None:
        try:
            await pending.client.disconnect()
        except Exception:
            pass  # best-effort cleanup - a half-torn-down client isn't worth failing the request over


def _reason(status_code: int, message: str, reason: str) -> HTTPException:
    """Same {"message", "reason"} shape as api/dependencies.py's HTTPExceptions -
    see web/js/lib/errors.js's describeError() for how the frontend turns `reason` into a translated string instead of showing `message` (always English) directly."""
    return HTTPException(status_code=status_code, detail={"message": message, "reason": reason})


@router.post("/credentials", response_model=TelegramCredentialsOut, summary="Save your Telegram app credentials")
def set_credentials(
    body: TelegramCredentialsIn,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramCredentialsOut:
    """
    Save this account's own api_id/api_hash (from my.telegram.org), encrypted at rest.

    Doesn't validate the pair against Telegram itself - that only happens for real on the next step,
    send-code, since api_id/api_hash can only actually be checked by trying to use them.
    A non-numeric api_id is rejected here (400) since that's a client-side mistake this schema CAN catch cheaply;
    a numeric-but-wrong api_id is Telegram's problem to report, not this endpoint's to guess at.

    Callable again later to replace existing credentials - see set_telegram_credentials()'s own docstring (control_db/queries.py) for what doing so clears.
    """
    try:
        api_id = int(body.api_id)
    except ValueError:
        raise _reason(400, "api_id must be numeric.", "invalid_api_credentials")

    encrypted_api_id = encrypt_secret(str(api_id))
    encrypted_api_hash = encrypt_secret(body.api_hash)
    control_db.queries.set_telegram_credentials(control_conn, current["user_id"], encrypted_api_id, encrypted_api_hash)
    return TelegramCredentialsOut()


@router.post("/link/send-code", response_model=TelegramSendCodeOut, summary="Start Telegram verification for your own phone number")
async def send_code(
    body: TelegramSendCodeIn,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramSendCodeOut:
    """
    Send a login code to `phone` via Telegram, using the api_id/api_hash this account already saved through POST /telegram/credentials.

    Discards any earlier still-pending handshake for this account first -
    calling send-code again is how you restart after a mistake (wrong phone, expired code, whatever), not something that stacks.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    if user is None or user["telegram_api_id"] is None or user["telegram_api_hash"] is None:
        raise _reason(
            409,
            "Save your Telegram api_id/api_hash first (POST /telegram/credentials) before requesting a code.",
            "telegram_credentials_missing",
        )

    api_id = int(decrypt_secret(user["telegram_api_id"]))
    api_hash = decrypt_secret(user["telegram_api_hash"])

    await _discard_pending_link(current["user_id"])

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(body.phone)
    except ApiIdInvalidError:
        await client.disconnect()
        raise _reason(400, "The saved api_id/api_hash pair isn't valid.", "invalid_api_credentials")
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise _reason(400, "That phone number isn't valid.", "telegram_invalid_phone")
    except FloodWaitError:
        await client.disconnect()
        raise _reason(429, "Telegram is asking us to slow down. Wait a few minutes before trying again.", "telegram_flood_wait")

    async with _lock:
        _pending_links[current["user_id"]] = _PendingLink(client, body.phone, sent.phone_code_hash)
    return TelegramSendCodeOut()


@router.post("/link/confirm", response_model=TelegramConfirmOut, summary="Confirm the Telegram code (and password, if asked for)")
async def confirm(
    body: TelegramConfirmIn,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramConfirmOut:
    """
    Second half of the handshake started by send-code.

    First call (right after send-code): body.code set, body.password omitted.
    If the account has no 2FA password, this alone finishes the link.
    If it does, Telegram raises SessionPasswordNeededError - the pending link is kept open (awaiting_password=True) rather than discarded,
    and the response comes back {"linked": false, "needs_password": true} instead of an error, since this is an expected fork in the flow, not a failure.

    Second call (only if the first said needs_password):
    body.password set, body.code omitted - Telegram doesn't need the code again, just the password, against the SAME still-connected client.

    Either call finishing successfully: the session string is extracted, encrypted, and saved - the pending link is discarded either way, success or final failure.
    """
    async with _lock:
        pending = _pending_links.get(current["user_id"])

    if pending is None or pending.expired:
        await _discard_pending_link(current["user_id"])
        raise _reason(409, "No verification is in progress. Start again with /telegram/link/send-code.", "telegram_no_pending_link")

    if pending.attempts >= MAX_CONFIRM_ATTEMPTS:
        await _discard_pending_link(current["user_id"])
        raise _reason(429, "Too many attempts. Start again with /telegram/link/send-code.", "telegram_too_many_attempts")

    try:
        if pending.awaiting_password:
            if not body.password:
                raise _reason(400, "This account needs its Telegram password to finish linking.", "telegram_password_required")
            await pending.client.sign_in(password=body.password)
        else:
            if not body.code:
                raise _reason(400, "The Telegram code is required.", "telegram_code_required")
            try:
                await pending.client.sign_in(pending.phone, code=body.code, phone_code_hash=pending.phone_code_hash)
            except SessionPasswordNeededError:
                pending.awaiting_password = True
                return TelegramConfirmOut(linked=False, needs_password=True)
    except (PhoneCodeInvalidError, PasswordHashInvalidError):
        pending.attempts += 1
        reason = "telegram_invalid_password" if pending.awaiting_password else "telegram_invalid_code"
        raise _reason(400, "That wasn't correct.", reason)
    except PhoneCodeExpiredError:
        await _discard_pending_link(current["user_id"])
        raise _reason(400, "That code expired. Start again with /telegram/link/send-code.", "telegram_code_expired")
    except FloodWaitError:
        raise _reason(429, "Telegram is asking us to slow down. Wait a few minutes before trying again.", "telegram_flood_wait")

    # Signed in - extract, encrypt, and save the session string, then tear down the in-memory client.
    session_string = pending.client.session.save()
    control_db.queries.set_telegram_session(control_conn, current["user_id"], encrypt_secret(session_string))
    await _discard_pending_link(current["user_id"])
    return TelegramConfirmOut(linked=True)