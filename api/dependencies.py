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

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import control_db
import db
from config import settings
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
        raise HTTPException(
            status_code=503,
            detail=(
                f"Database unavailable: {exc}. "
                "Ensure Postgres is running and the TeleVault userbot has run at least once."
            ),
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
    """
    try:
        conn = control_db.get_connection()
    except (OperationalError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Control database unavailable: {exc}") from exc

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


def require_owner(current: DecodedAccessToken = Depends(get_current_user)) -> DecodedAccessToken:
    """
    Gate for every route that touches THIS instance's archive or archiver
    (chats/messages/deleted/stats/telethon/backfill - applied at the router level in api/server.py, not per-route here).
    Only the one account named by settings.owner_user_id may pass.

    Deliberately NOT "any authenticated user" and NOT "any admin" - see config.py's owner_user_id docstring for the full reasoning, but in short:
    this archive is one person's private message history. is_admin answers a completely different question
    ("can this account manage other accounts") and must never double as "can this account read someone else's messages" -
    an admin promoted later (see scripts/manage_admin.py) should never gain access to an archive just by being an admin.
    Ownership and adminship are independent; this dependency checks only the former.

    Raises HTTPException 403 for any authenticated-but-wrong account (a valid token that isn't the owner's) -
    distinct from get_current_user()'s 401 for no-token-at-all/invalid-token,
    so a client can tell "you're not logged in" apart from "you're logged in as the wrong account".
    """
    if current["user_id"] != settings.owner_user_id:
        raise HTTPException(status_code=403, detail="This account does not have access to this archive.")
    return current