"""
FastAPI dependencies shared across route modules.

get_db() yields a read-only Postgres connection for the duration of one request and closes it
(returning it to the pool) in the finally block - whether the request succeeds or raises an exception.

Why read-only?
    Enforced via db.get_readonly_connection() (see db/connection.py), which issues `SET default_transaction_read_only = on` on checkout.
    Any write attempted through this connection errors out at the Postgres level - the same safeguard the old SQLite `file:path?mode=ro` URI mode gave,
    just enforced by the server instead of the driver.
    Matters for the same reason it did before:
    the userbot and the API server are separate processes sharing one database, and a route handler has no business writing to it.

Previously (SQLite era) this module opened a brand-new raw connection per request,
since SQLite connections are cheap and pooling added complexity without benefit at personal-tool scale.
Under Postgres, "new connection per request" would mean a fresh TCP handshake + auth round-trip per API call -
this now checks out a connection from the shared pool instead
(still one per request conceptually, just backed by a pool so the underlying network connections are reused rather than opened and torn down every time).
"""

from typing import Generator
import logging

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import control_db
import db

logger = logging.getLogger(__name__)
from utils.security import DecodedAccessToken, decode_access_token


def get_db() -> Generator[Connection, None, None]:
    """
    Yield a read-only pooled connection for one request, then return it to the pool.

    Usage in a route:
        from fastapi import Depends
        from api.dependencies import get_db

        @router.get("/example")
        def example(db: Connection = Depends(get_db)):
            ...

    Raises:
        HTTPException 503 if the database is unreachable (e.g. Postgres isn't running, or the userbot has never run so nothing has been migrated/created yet).
        `detail` is a {"message": <english>, "reason": "db_unavailable"} dict, not a bare string - see get_archive_connection()'s own docstring for why
        (short version: the raw OperationalError text is for the server log, not an end user's screen).

    Implementation note: the read-only context manager's __enter__/__exit__ are driven manually
    (rather than a plain `with` wrapping the whole function body)
    so that only the connection-open step is covered by the try/except below - an exception raised later,
    while the route handler is actually using the connection, must NOT be swallowed and reported as "database unavailable";
    it should propagate as whatever error it is.
    """
    cm = db.get_readonly_connection()
    try:
        conn = cm.__enter__()
    except OperationalError as exc:
        logger.warning("Primary database is unreachable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "The database is currently unavailable. Ensure Postgres is running and reachable.",
                "reason": "db_unavailable",
            },
        ) from exc

    try:
        yield conn
    finally:
        cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Control DB (auth/multi-user feature)
# ---------------------------------------------------------------------------

def get_control_db() -> Generator[Connection, None, None]:
    """
    Yield a read-write pooled connection to the control DB for one request, then return it to the pool.

    No read-only variant here, unlike get_db() above - see control_db/connection.py's module
    docstring for why the control DB has no reader/writer split to enforce in the first place.

    Raises HTTPException 503 on the same "database unreachable" condition get_db() guards against, for the same reason (e.g. Postgres isn't running yet).
    Same structured-detail reasoning as get_db() above; reason code is "control_db_unavailable" instead,
    since the frontend may eventually want to tell the two apart (this one affects every account, not just accounts sharing a particular archive).
    """
    try:
        conn = control_db.get_connection()
    except (OperationalError, RuntimeError) as exc:
        logger.warning("Control database is unreachable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "The database is currently unavailable. Try again shortly.",
                "reason": "control_db_unavailable",
            },
        ) from exc

    try:
        yield conn
    finally:
        conn.close()


# OAuth2PasswordBearer just tells FastAPI/Swagger where a client would go to obtain a token (powers the /api/docs "Authorize" button) -
# it does not itself call that endpoint or validate anything;
# it only extracts the raw bearer string from the Authorization header for us below.
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(token: str = Depends(_oauth2_scheme)) -> DecodedAccessToken:
    """
    Decode and verify the caller's access token. Does NOT touch the database.

    That's deliberate, not an oversight - see utils/security.py's module docstring:
    an access token's whole purpose is to authorize a request without a per-request DB round trip.
    In particular this means a user deleted or locked AFTER their access token was issued stays authorized until that token naturally expires
    (ACCESS_TOKEN_MINUTES, currently 15) - locking an account takes effect immediately for /auth/refresh
    (which does check the database) and so within one access-token lifetime everywhere else, not instantly on every request.

    Raises HTTPException 401 on anything wrong with the token (expired, forged, wrong type) -
    jwt.InvalidTokenError and its subclasses (including ExpiredSignatureError) all land here.
    """
    try:
        return decode_access_token(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_archive_connection(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> Generator[Connection, None, None]:
    """
    Resolve the CALLING user's own archive (control_db.schema.users.archive_db_ref) and yield a read-only connection to it.
    Used by chats/messages/deleted/stats in place of a fixed get_db() call - each user sees their own archive, not a single instance-wide one.

    Replaces the earlier require_owner design (a single settings.owner_user_id gate on one static archive):
    that assumed exactly one archive per running instance,
    which isn't the actual model - every user is meant to get their own archive_db_ref once they finish linking Telegram
    (see the confirmed design note reproduced in db/connection.py's per-tenant-connections docstring).
    is_admin plays NO role in this check, on purpose - admin status governs account management, never archive access
    (see scripts/manage_admin.py's own docstring for why an admin promoted later must never gain another user's messages just by being an admin).

    Raises:
        HTTPException 409 if this account has no archive_db_ref yet - NOT a 403,
        because this isn't "you don't have access", it's "there is nothing here to have access to yet".
        The distinct status lets the frontend show "finish linking Telegram" rather than "access denied".
        HTTPException 503 if the archive database exists as a reference but isn't actually reachable right now
        (e.g. Postgres restarted, or - once real provisioning exists - mid-provisioning).

    Both exceptions' `detail` is a {"message": <english>, "reason": <code>} dict, not a bare string - see web/js/lib/errors.js's describeError(),
    which maps `reason` to a translated string for the UI instead of showing `message` (English-only) directly.
    The 503 case deliberately does NOT include the underlying OperationalError's own text in `message` - psycopg's connection-failure messages are long,
    mention internal hostnames/ports, and mean nothing to an end user;
    the real exception is logged server-side instead, for whoever's actually debugging it.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    if user is None or user["archive_db_ref"] is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive hasn't been set up yet. Finish linking your Telegram account to provision it.",
                "reason": "archive_unattached",
            },
        )

    cm = db.get_tenant_readonly_connection(user["archive_db_ref"])
    try:
        conn = cm.__enter__()
    except OperationalError as exc:
        logger.warning("Archive %r (user_id=%s) is unreachable: %s", user["archive_db_ref"], current["user_id"], exc)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Your archive database is currently unavailable.",
                "reason": "archive_unavailable",
            },
        ) from exc

    try:
        yield conn
    finally:
        cm.__exit__(None, None, None)


def require_instance_owner(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> DecodedAccessToken:
    """
    Gate for telethon.py/backfill.py: controlling THIS running process's one live userbot
    (main.py - a single Telethon session against a single database_url) is inherently a single-owner action today,
    since there is only one physical userbot for this instance to control, regardless of how many control_db accounts exist.
    Only the account whose own archive_db_ref matches this instance's actual database_url may start/stop/backfill it.

    This is NOT the same question get_archive_connection() answers (which archive should I read FROM),
    even though today, for the one account this is true of, both happen to resolve to the same database -
    they're independent checks that will diverge the moment real per-user provisioning exists and a second physical userbot process serves a second user:
    that second user's get_archive_connection() would resolve to their own database,
    but they would never pass require_instance_owner() on THIS process, because this process's userbot was never theirs to control in the first place.

    Raises HTTPException 403 - with a `detail` that distinguishes WHY,
    since they're different situations for the person seeing them (see web/js/lib/errors.js's describeError()):
        - archive_unattached: this account has no archive_db_ref at all yet - same reason code get_archive_connection() uses for the identical underlying fact,
          so the two surfaces show the same message rather than two different-sounding explanations of one situation.
        - not_instance_owner: this account DOES have an archive, it's just not the one this running process's userbot serves.
          This is the ordinary, permanent, expected state for every account except the one instance owner - not an error to fix, just not this account's feature.
    There's deliberately no third "archive_ref matches but that database is unreachable" case here:
    this function only compares archive_db_ref to database_url as strings - it never opens a connection, so it cannot observe that failure mode.
    If the instance owner's own database goes down, that surfaces wherever the route handler itself actually connects, not here.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    instance_db_name = db.get_primary_database_name()

    if user is None or user["archive_db_ref"] is None:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Your archive hasn't been set up yet. Finish linking your Telegram account to provision it.",
                "reason": "archive_unattached",
            },
        )
    if user["archive_db_ref"] != instance_db_name:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "This account does not control this instance's Telegram connection.",
                "reason": "not_instance_owner",
            },
        )
    return current