"""
FastAPI dependencies shared across route modules.

Currently provides a single dependency: get_db(),
which yields a read-only Postgres connection for the duration of one request and closes it
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

from fastapi import HTTPException
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import db


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