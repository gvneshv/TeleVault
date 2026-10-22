"""
Managing an account's OTHER Telegram sessions ("devices") and ending TeleVault's own - the part of the web UI's Settings that used to be "do it in Telegram yourself".

Why this exists:
    Every link (QR or login code, first time or relink, same computer or not) creates a NEW authorization in the person's Telegram account,
    shown under Telegram's Settings -> Devices.
    TeleVault only ever stored the session; nothing ever ended one, so they piled up.
    The Telegram API has first-class calls for this (account.getAuthorizations, account.resetAuthorization, auth.resetAuthorizations, auth.logOut),
    so this module is the thin, testable layer between those calls and api/routes/telegram_sessions.py / the unlink route.

Everything here acts AS the account: it connects with the account's own stored session string (the same secret a worker uses, see utils/account.py).
Two consequences the callers must respect:
  * The session string is equivalent to being logged in as that person - it is never logged, returned, or put in an exception message here.
  * Only one process should use a session at a time (the same rule the backfill/archiver mutual exclusion enforces),
    so the ROUTES refuse while the account's archiver or a backfill is running (api.process_utils.running_process_reason) before calling anything in here.

Privacy: the device list contains IP addresses and locations of the person's own sessions.
They are handed to the owning account only and never logged - log lines in here carry the user id and counts, nothing else.

Telegram rules worth knowing (from Telegram's API documentation; not re-verified for every error name, see the classification below):
  * The CURRENT session (the one making the request - i.e. TeleVault's own) cannot be ended with resetAuthorization; ending it is auth.logOut.
  * Ending OTHER sessions is refused (FRESH_RESET_AUTHORISATION_FORBIDDEN) while the current session is young - Telegram documents about 24 hours.
    That surfaces here as reason "telegram_session_too_new" so the UI can explain it instead of showing a bare failure.
"""

import asyncio
import logging
from typing import Any
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    HashInvalidError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest

from utils.telethon_compat import disconnect_client

logger = logging.getLogger(__name__)

# One Telegram round trip (connect, or a single request) may take at most this long before we report "unreachable" instead of hanging the HTTP request.
TELEGRAM_CALL_TIMEOUT_SECONDS = 20

# Telethon errors meaning "this stored session no longer works" (ended from another device, account deactivated, key used from two places).
# The person has to link again; nothing here can fix it.
_DEAD_SESSION_ERRORS = (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    SessionRevokedError,
    SessionExpiredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


class TelegramSessionError(Exception):
    """A failure the route turns into an HTTP error: `status_code`, an English `message`, and a machine-readable `reason` the web UI translates."""

    def __init__(self, status_code: int, message: str, reason: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.reason = reason


@dataclass(frozen=True)
class TelegramDevice:
    """One entry of Telegram's own device list.
    `device_hash` is Telegram's id for the session (an int64 - a string by the time it reaches JSON, JS cannot hold it)."""

    device_hash: int
    current: bool  # True for the session that made the request, i.e. TeleVault's own
    device_model: str
    platform: str
    system_version: str
    app_name: str
    app_version: str
    official_app: bool
    created_at: datetime | None
    active_at: datetime | None
    ip: str
    country: str
    region: str


def _translate(exc: BaseException) -> TelegramSessionError:
    """Map a Telethon/network exception to the error the API reports.
    Unknown exceptions are NOT mapped (they propagate as the 500 they are)."""
    if isinstance(exc, _DEAD_SESSION_ERRORS):
        return TelegramSessionError(
            409,
            "Telegram no longer accepts TeleVault's saved session (it was ended elsewhere). Link your Telegram account again.",
            "telegram_session_invalid",
        )
    if isinstance(exc, FreshResetAuthorisationForbiddenError):
        return TelegramSessionError(
            409,
            "Telegram does not allow ending other devices yet: TeleVault's own session is too new (Telegram requires it to be about a day old).",
            "telegram_session_too_new",
        )
    if isinstance(exc, HashInvalidError):
        return TelegramSessionError(404, "That device no longer exists in Telegram.", "telegram_device_not_found")
    if isinstance(exc, FloodWaitError):
        return TelegramSessionError(429, "Telegram is asking us to slow down. Wait a few minutes before trying again.", "telegram_flood_wait")
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return TelegramSessionError(502, "Could not reach Telegram right now. Try again shortly.", "telegram_unreachable")
    raise exc


@asynccontextmanager
async def _client_for(api_id: int, api_hash: str, session_string: str) -> AsyncIterator[TelegramClient]:
    """
    Connect as the account for the duration of one operation, translating Telegram/network failures to TelegramSessionError and always disconnecting.

    A fresh client per operation (nothing is kept between HTTP requests) so no connection to Telegram outlives the request that needed it.
    """
    try:
        # StringSession() itself raises on a corrupted saved string (bad base64 / wrong length) - the same trap main.py's startup comment describes.
        # That is a LOCAL problem (nothing was sent to Telegram), reported separately from "Telegram rejected the session" so unlink does not claim it ended anything.
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
    except ValueError as exc:  # binascii.Error (bad base64) is a ValueError subclass
        raise TelegramSessionError(
            409,
            "TeleVault's saved Telegram session is unreadable. Link your Telegram account again.",
            "telegram_session_unreadable",
        ) from exc
    try:
        try:
            await asyncio.wait_for(client.connect(), TELEGRAM_CALL_TIMEOUT_SECONDS)
            yield client
        except TelegramSessionError:
            raise
        except Exception as exc:  # noqa: BLE001 - everything is classified in _translate(); unknown errors are re-raised there
            raise _translate(exc) from exc
    finally:
        await disconnect_client(client)


async def _call(client: TelegramClient, request: Any) -> Any:
    """One Telegram API call with the shared timeout.
    Typed Any on purpose: Telethon's stubs infer a list for client(request), which is wrong for these requests."""
    return await asyncio.wait_for(client(request), TELEGRAM_CALL_TIMEOUT_SECONDS)


def _device(a) -> TelegramDevice:
    """Build our record from Telethon's Authorization, tolerating fields a future Telegram layer drops (getattr defaults)."""
    return TelegramDevice(
        device_hash=int(a.hash),
        current=bool(getattr(a, "current", False)),
        device_model=str(getattr(a, "device_model", "") or ""),
        platform=str(getattr(a, "platform", "") or ""),
        system_version=str(getattr(a, "system_version", "") or ""),
        app_name=str(getattr(a, "app_name", "") or ""),
        app_version=str(getattr(a, "app_version", "") or ""),
        official_app=bool(getattr(a, "official_app", False)),
        created_at=getattr(a, "date_created", None),
        active_at=getattr(a, "date_active", None),
        ip=str(getattr(a, "ip", "") or ""),
        country=str(getattr(a, "country", "") or ""),
        region=str(getattr(a, "region", "") or ""),
    )


async def _list(client: TelegramClient) -> list[TelegramDevice]:
    result = await _call(client, GetAuthorizationsRequest())
    return [_device(a) for a in result.authorizations]


async def list_devices(api_id: int, api_hash: str, session_string: str) -> list[TelegramDevice]:
    """All sessions of the account, TeleVault's own (current=True) included."""
    async with _client_for(api_id, api_hash, session_string) as client:
        return await _list(client)


async def end_device(api_id: int, api_hash: str, session_string: str, device_hash: int) -> None:
    """
    End ONE other session.

    Looks the device up first instead of just sending the request: that is what lets us refuse TeleVault's own session
    (400 - ending it would silently unlink the account; the explicit way is "Unlink Telegram") and answer an unknown hash with a clean 404.
    """
    async with _client_for(api_id, api_hash, session_string) as client:
        devices = await _list(client)
        target = next((d for d in devices if d.device_hash == device_hash), None)
        if target is None:
            raise TelegramSessionError(404, "That device no longer exists in Telegram.", "telegram_device_not_found")
        if target.current:
            raise TelegramSessionError(
                400,
                "That is TeleVault's own session. Use \"Unlink Telegram\" to end it.",
                "telegram_cannot_end_current",
            )
        await _call(client, ResetAuthorizationRequest(hash=device_hash))


async def end_other_devices(api_id: int, api_hash: str, session_string: str) -> None:
    """End every session of the account except TeleVault's own (Telegram's auth.resetAuthorizations)."""
    async with _client_for(api_id, api_hash, session_string) as client:
        await _call(client, ResetAuthorizationsRequest())


async def log_out(api_id: int, api_hash: str, session_string: str) -> bool:
    """
    End TeleVault's OWN session in Telegram (auth.logOut), for "Unlink Telegram".

    Returns True when the session is gone afterwards - either we ended it, or Telegram already considered it dead (nothing left to end).
    Returns False when that could not be established (Telegram unreachable, rate limited, ...): the caller still unlinks locally -
    a person must always be able to disconnect from our side - and tells the person to remove the entry in Telegram themselves.
    Never raises for Telegram/network problems; an unexpected programming error still propagates.
    """
    try:
        async with _client_for(api_id, api_hash, session_string) as client:
            await asyncio.wait_for(client.log_out(), TELEGRAM_CALL_TIMEOUT_SECONDS)
        return True
    except TelegramSessionError as exc:
        if exc.reason == "telegram_session_invalid":
            return True
        logger.warning("Could not end the Telegram session during unlink (%s)", exc.reason)
        return False
