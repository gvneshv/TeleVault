"""
FastAPI dependencies shared across route modules.

The API server never opens "the" archive database - it serves every account at once,
so each request resolves the CALLING account's own archive (get_archive_connection() below) and yields a read-only connection to it
(db.get_tenant_readonly_connection(), see db/connection.py), closing it in the finally block whether the request succeeds or raises.

Why read-only?
    Enforced via `SET default_transaction_read_only = on` on checkout.
    Any write attempted through the connection errors out at the Postgres level.
    The workers (main.py, backfill.py) and the API server are separate processes that can touch the same archive database,
    and a route handler has no business writing to it.
    The deliberate exceptions are few and each one is explicit: cancel_backfill() opens its own write connection,
    and the ingest blocklist's add/remove routes (api/routes/ingest.py)
    and the "delete this chat's data" route (DELETE /api/chats/{chat_id}) use get_archive_write_connection() below -
    the same account-resolution and error mapping as get_archive_connection(), but read-write.
    Everything that merely READS an archive keeps using the read-only dependency, so a bug in a read route still cannot modify archived data.
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
from utils.archive_login import ArchiveLoginError, archive_login_from_row

logger = logging.getLogger(__name__)
from utils.security import DecodedAccessToken, decode_access_token


# ---------------------------------------------------------------------------
# Control DB (auth/multi-user feature)
# ---------------------------------------------------------------------------

def get_control_db() -> Generator[Connection, None, None]:
    """
    Yield a read-write pooled connection to the control DB for one request, then return it to the pool.

    No read-only variant here - see control_db/connection.py's module docstring for why the control DB has no reader/writer split to enforce in the first place.

    Raises HTTPException 503 when the database is unreachable (e.g. Postgres isn't running yet).
    `detail` is a {"message", "reason"} dict rather than a bare string - see get_archive_connection()'s docstring for why;
    the reason is "control_db_unavailable" (it affects every account, not just one archive).
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


def archive_target_for_user(user: dict) -> tuple[str, db.ArchiveLogin | None]:
    """
    (archive database name, that archive's own login) from a control-DB users row whose archive_db_ref is set.
    The login is None for an archive that has not been given its own Postgres role yet (it is then reached with the server's login) - see db/connection.py.
    If a stored login cannot be decrypted this raises HTTPException 503 "archive_unavailable" rather than falling back to the server's login:
    quietly using the all-powerful login is exactly what per-archive roles exist to prevent.
    Shared by every API route that opens an archive, so they all pick the database and the login from the same row in the same way.
    """
    try:
        login = archive_login_from_row(user)
    except ArchiveLoginError as exc:
        logger.error("Archive %r (user_id=%s): %s", user["archive_db_ref"], user["id"], exc)
        raise HTTPException(
            status_code=503,
            detail={"message": "Your archive database is currently unavailable.", "reason": "archive_unavailable"},
        ) from exc
    return user["archive_db_ref"], login


def _archive_target_or_409(current: DecodedAccessToken, control_conn: Connection) -> tuple[str, db.ArchiveLogin | None]:
    """
    The calling account's archive (database name from users.archive_db_ref, plus its own login), or HTTPException 409 "archive_unattached" if it has none yet.
    Shared by get_archive_connection() and get_archive_write_connection() so both gate on exactly the same thing, with exactly the same wording.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    if user is None or user["archive_db_ref"] is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database hasn't been created yet. Create it under Settings -> Telegram setup.",
                "reason": "archive_unattached",
            },
        )
    return archive_target_for_user(user)


def _archive_connect_error(exc: OperationalError, archive_db_ref: str, user_id: int) -> HTTPException:
    """
    The HTTPException for "could not connect to this account's archive" - see get_archive_connection()'s docstring for the 409 vs 503 reasoning.
    The underlying exception is logged here (it names internal hosts/ports) and deliberately never put in the response.
    """
    if db.is_missing_database_error(exc):
        # archive_db_ref names a database that was never actually created (contrast the generic 503 below, a transient outage that WILL clear up on its own) -
        # see db.is_missing_database_error()'s own docstring.
        # This can only be fixed by an administrator (create the database, or correct the reference);
        # it must never be told to the caller as "try again shortly", since retrying cannot possibly help.
        logger.error("Archive %r (user_id=%s) does not exist - misconfigured archive_db_ref: %s", archive_db_ref, user_id, exc)
        return HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database reference is misconfigured and cannot be fixed by retrying. Contact your administrator.",
                "reason": "archive_misconfigured",
            },
        )
    logger.warning("Archive %r (user_id=%s) is unreachable: %s", archive_db_ref, user_id, exc)
    return HTTPException(
        status_code=503,
        detail={
            "message": "Your archive database is currently unavailable.",
            "reason": "archive_unavailable",
        },
    )


def get_archive_connection(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> Generator[Connection, None, None]:
    """
    Resolve the CALLING user's own archive (control_db.schema.users.archive_db_ref) and yield a read-only connection to it.
    Used by chats/messages/deleted/stats/backfill-history - each user sees their own archive, never a shared one.
    Every user gets their own archive_db_ref once they finish setup (POST /archive/provision) - there is no instance-wide archive.
    is_admin plays NO role in this check, on purpose - admin status governs account management, never archive access
    (see scripts/manage_admin.py's own docstring for why an admin promoted later must never gain another user's messages just by being an admin).

    Raises:
        HTTPException 409 if this account has no archive_db_ref yet - NOT a 403,
        because this isn't "you don't have access", it's "there is nothing here to have access to yet".
        The distinct status lets the frontend show a "create your archive database" prompt rather than "access denied".
        (It is NOT about Telegram: an account can be fully linked and still land here,
        because linking Telegram and creating the archive database are two separate steps - see api/routes/archive.py.)
        HTTPException 503 if the archive database exists as a reference but isn't actually reachable right now
        (e.g. Postgres restarted, or - once real provisioning exists - mid-provisioning).
        HTTPException 409 with reason "archive_misconfigured" if archive_db_ref names a database that was never actually created
        (see db.is_missing_database_error()'s own docstring for how this is told apart from the 503 case above) -
        a permanent admin-fix-it situation, not a "try again shortly" one,
        so it gets its own reason code rather than collapsing into "archive_unavailable" and telling someone to retry something retrying can never fix.

    Both exceptions' `detail` is a {"message": <english>, "reason": <code>} dict, not a bare string - see web/js/lib/errors.js's describeError(),
    which maps `reason` to a translated string for the UI instead of showing `message` (English-only) directly.
    The 503 case deliberately does NOT include the underlying OperationalError's own text in `message` - psycopg's connection-failure messages are long,
    mention internal hostnames/ports, and mean nothing to an end user;
    the real exception is logged server-side instead, for whoever's actually debugging it.
    """
    archive_db_ref, login = _archive_target_or_409(current, control_conn)

    cm = db.get_tenant_readonly_connection(archive_db_ref, login)
    try:
        conn = cm.__enter__()
    except OperationalError as exc:
        raise _archive_connect_error(exc, archive_db_ref, current["user_id"]) from exc

    try:
        yield conn
    finally:
        cm.__exit__(None, None, None)


def get_archive_write_connection(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> Generator[Connection, None, None]:
    """
    Like get_archive_connection(), but READ-WRITE:
    for the few routes that legitimately change an archive (today the ingest blocklist's add/remove, api/routes/ingest.py, and purging a chat, api/routes/chats.py).
    Same account resolution (the caller's own archive, never another's, admin or not) and the same 409/503 errors.

    Use it only where a route must write - every route that merely reads keeps the read-only dependency,
    so a bug there still cannot modify archived data (see this module's docstring).
    The caller commits its own changes; whatever is uncommitted when the request ends is discarded when the connection is closed.
    """
    archive_db_ref, login = _archive_target_or_409(current, control_conn)

    try:
        conn = db.get_tenant_connection(archive_db_ref, login)
    except OperationalError as exc:
        raise _archive_connect_error(exc, archive_db_ref, current["user_id"]) from exc

    try:
        yield conn
    finally:
        conn.close()


def require_admin(current: DecodedAccessToken = Depends(get_current_user)) -> DecodedAccessToken:
    """
    Gate for api/routes/admin.py: the one admin account only
    (single-admin model - see control_db.schema.ix_users_single_admin and control_db.queries.admin_exists() for the enforcement side of that).

    Reads is_admin straight off the ALREADY-DECODED access token (get_current_user(), no DB round trip) rather than looking the user up fresh here -
    same "an access token's whole purpose is to authorize a request without a per-request DB round trip" reasoning get_current_user()'s own docstring gives,
    and the same up-to-15-minutes-stale tradeoff it already documents:
    an admin locked or deleted by a THEORETICAL second admin (impossible today - see above) would keep passing this check until their access token naturally expires,
    exactly as every other claim on this token already does.
    Every route behind this dependency also calls control_db.queries functions that re-check the target row for themselves
    (e.g. delete_user_completely() looking the user up fresh),
    so nothing downstream trusts this token claim alone for anything destructive beyond "may this caller reach this endpoint at all".

    Raises HTTPException 403: being a non-admin is an ordinary, permanent fact about most accounts, not a transient error to recover from.
    """
    if not current["is_admin"]:
        raise HTTPException(
            status_code=403,
            detail={"message": "This action requires admin privileges.", "reason": "not_admin"},
        )
    return current


def require_archiving_ready(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> DecodedAccessToken:
    """
    Gate for STARTING an archiver or a backfill (api/routes/telethon.py's start, api/routes/backfill.py's start):
    the calling account must have finished setup - Telegram linked (credentials AND a signed-in session) and an archive database provisioned.

    This replaced require_instance_owner(), which only let through the one account whose archive happened to match a single instance-wide DATABASE_URL.
    That was a leftover of the single-user design.
    Now every account runs its own workers (see utils/account.py), so the only question is "can THIS account's worker actually run?" -
    answered here with an explicit reason instead of a late crash in a subprocess nobody is watching.

    Raises HTTPException 409 (not 403: nothing is forbidden, something just isn't set up yet - same stance as get_archive_connection()),
    with a `detail` the frontend can translate (web/js/lib/errors.js):
        - telegram_not_linked: no saved API credentials or no signed-in session yet.
        - archive_unattached:  Telegram is linked but there is no archive database yet (same code get_archive_connection() uses for the identical fact).
        - account_locked (403): an admin locked this account.
          Only reachable within one access-token lifetime of the lock (see get_current_user()'s note) -
          sign-in and refresh already refuse locked accounts - but a worker must never start for a locked account, so it is checked here too.
    Never opens a connection to the archive itself, so it cannot observe an unreachable archive database - that surfaces in the worker's own log.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    if user is None or user["is_locked"]:
        raise HTTPException(
            status_code=403,
            detail={"message": "This account is locked.", "reason": "account_locked"},
        )
    if not (user["telegram_api_id"] and user["telegram_api_hash"] and user["telegram_session_string"]):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Link your Telegram account first (Settings -> Telegram setup).",
                "reason": "telegram_not_linked",
            },
        )
    if user["archive_db_ref"] is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database hasn't been created yet. Create it under Settings -> Telegram setup.",
                "reason": "archive_unattached",
            },
        )
    return current