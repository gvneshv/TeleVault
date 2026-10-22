"""
Telegram device management: GET /telegram/sessions, DELETE /telegram/sessions/{id}, DELETE /telegram/sessions.

What the person sees: the "Telegram devices" card in the web UI's Settings tab - the same list as Telegram's own Settings -> Devices,
with the buttons to end one device or every device except TeleVault's own.
(Ending TeleVault's own session is "Unlink Telegram", DELETE /telegram/session in api/routes/telegram.py, which now ends it in Telegram too.)

All the Telegram work lives in utils/telegram_sessions.py (see its docstring for the Telegram rules and the privacy stance);
this module only does what an HTTP route has to:
authentication, "is it safe to connect as this account right now", and turning TelegramSessionError into the usual {message, reason} error body.

Guards, in order:
  1. The account must be linked (409 telegram_not_linked) - there is no session to act as otherwise.
  2. Its archiver and any backfill must not be running (409, the same reasons unlink uses):
  they hold this very session, and one session is used by one process at a time.
     This is checked right before connecting, not enforced afterwards -
     a worker started in the few seconds between the check and the end of our request is a race we accept:
     the worst case is Telegram invalidating the session (the worker then exits with the usual "link again" message), the same outcome as any other session end.
  3. One management operation per account at a time (an asyncio lock):
  two tabs clicking at once would otherwise open two simultaneous Telegram connections with the same session.

No state is kept between requests and no connection to Telegram outlives a request.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.engine import Connection

import control_db
from api.dependencies import get_control_db, get_current_user
from api.process_utils import running_process_reason
from api.schemas import TelegramDeviceEndedOut, TelegramDeviceOut, TelegramDevicesOut
from utils import telegram_sessions
from utils.crypto import decrypt_secret
from utils.security import DecodedAccessToken
from utils.telegram_sessions import TelegramDevice, TelegramSessionError
from utils.validation import TELEGRAM_DEVICE_HASH_MAX_LENGTH, TELEGRAM_DEVICE_HASH_PATTERN
from utils.worker_paths import backfill_status_path, heartbeat_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram/sessions", tags=["telegram"])

# One lock per account that has ever managed devices.
# The dict only grows by one small object per such account, which is not worth a cleanup scheme.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    return _locks.setdefault(user_id, asyncio.Lock())


def _http(exc: TelegramSessionError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"message": exc.message, "reason": exc.reason})


def _linked_account_or_409(control_conn: Connection, user_id: int) -> tuple[int, str, str]:
    """(api_id, api_hash, session_string) of an account that is fully linked and idle, or the 409 that says why not.
    Decrypted values never leave this call chain."""
    user = control_db.queries.get_user_by_id(control_conn, user_id)
    if (
        user is None
        or user["telegram_api_id"] is None
        or user["telegram_api_hash"] is None
        or user["telegram_session_string"] is None
    ):
        raise HTTPException(
            status_code=409,
            detail={"message": "Link your Telegram account first (Settings -> Telegram setup).", "reason": "telegram_not_linked"},
        )
    reason = running_process_reason(heartbeat_path(user_id), backfill_status_path(user_id))
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Stop the userbot and any running backfill first - they are still using this Telegram session.",
                "reason": reason,
            },
        )
    api_id = decrypt_secret(user["telegram_api_id"])
    api_hash = decrypt_secret(user["telegram_api_hash"])
    session_string = decrypt_secret(user["telegram_session_string"])
    # decrypt_secret() maps None -> None; all three were just checked non-None, so these asserts only narrow the types for the checker.
    assert api_id is not None and api_hash is not None and session_string is not None
    return int(api_id), api_hash, session_string


def _device_out(d: TelegramDevice) -> TelegramDeviceOut:
    return TelegramDeviceOut(
        id=str(d.device_hash),
        current=d.current,
        device_model=d.device_model,
        platform=d.platform,
        system_version=d.system_version,
        app_name=d.app_name,
        app_version=d.app_version,
        official_app=d.official_app,
        created_at=d.created_at,
        active_at=d.active_at,
        ip=d.ip,
        country=d.country,
        region=d.region,
    )


@router.get("", response_model=TelegramDevicesOut, summary="List the devices (sessions) signed in to your Telegram account")
async def list_devices(
    response: Response,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramDevicesOut:
    """
    The account's Telegram sessions as Telegram reports them, TeleVault's own included (`current: true`).

    Contains IP addresses and locations, so it is never cached (`Cache-Control: no-store`) and nothing from it is logged.
    """
    api_id, api_hash, session_string = _linked_account_or_409(control_conn, current["user_id"])
    response.headers["Cache-Control"] = "no-store"
    async with _lock_for(current["user_id"]):
        try:
            devices = await telegram_sessions.list_devices(api_id, api_hash, session_string)
        except TelegramSessionError as exc:
            raise _http(exc)
    logger.info("Listed %d Telegram device(s) for user %d", len(devices), current["user_id"])
    return TelegramDevicesOut(devices=[_device_out(d) for d in devices])


@router.delete("/{device_hash}", response_model=TelegramDeviceEndedOut, summary="End one of your other Telegram devices")
async def end_device(
    device_hash: str = Path(..., max_length=TELEGRAM_DEVICE_HASH_MAX_LENGTH, pattern=TELEGRAM_DEVICE_HASH_PATTERN),
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramDeviceEndedOut:
    """
    End one session by the `id` GET /telegram/sessions returned.

    400 telegram_cannot_end_current for TeleVault's own session (that is "Unlink Telegram"), 404 if the device is already gone,
    409 telegram_session_too_new if Telegram refuses because TeleVault's own session is too young to end others.
    """
    api_id, api_hash, session_string = _linked_account_or_409(control_conn, current["user_id"])
    async with _lock_for(current["user_id"]):
        try:
            await telegram_sessions.end_device(api_id, api_hash, session_string, int(device_hash))
        except TelegramSessionError as exc:
            raise _http(exc)
    logger.info("Ended a Telegram device for user %d", current["user_id"])
    return TelegramDeviceEndedOut()


@router.delete("", response_model=TelegramDeviceEndedOut, summary="End every Telegram device except TeleVault's own")
async def end_other_devices(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> TelegramDeviceEndedOut:
    """
    End all other sessions at once. TeleVault's own session stays linked.

    Same errors as ending a single device (minus the 404 / "current" ones).
    Not undoable: every ended device has to sign in again.
    """
    api_id, api_hash, session_string = _linked_account_or_409(control_conn, current["user_id"])
    async with _lock_for(current["user_id"]):
        try:
            await telegram_sessions.end_other_devices(api_id, api_hash, session_string)
        except TelegramSessionError as exc:
            raise _http(exc)
    logger.info("Ended all other Telegram devices for user %d", current["user_id"])
    return TelegramDeviceEndedOut()
