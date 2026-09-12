"""
Manages the PostgreSQL connection pool for the entire application, via an SQLAlchemy Core Engine (psycopg v3 driver).

This replaces the old model of a single shared sqlite3.Connection held open for the app's entire lifetime.
SQLAlchemy's Engine owns a connection pool internally (QueuePool by default) and hands out individual connections on checkout
- this module wraps that in two functions matching what the two actual use cases need:

  - get_connection()          : a read-write connection for the userbot's live handlers and the backfill script.
  - get_readonly_connection() : a connection for the API server, with `SET default_transaction_read_only = on` applied immediately after checkout.

Why a pool instead of one global connection:
    The old sqlite3 approach worked because SQLite connections are cheap and a single-process app only ever has one in flight at a time.
    Postgres is a network service - checking out a pooled connection per unit of work
    (one handler invocation, one API request) and returning it afterwards is the normal, correct pattern, and it's what makes concurrent use safe.

Why get_connection() is now "checkout, use, close" instead of "get the one open connection":
    This is a real contract change from the SQLite version, not just an implementation detail.
    Every caller of get_connection() must close the connection
    (or use it as a context manager: `with db.get_connection() as conn:`) to return it to the pool when done.
    The current handler call sites (handlers/on_message.py, on_delete.py, on_edit.py)
    still call get_connection() expecting the old "one connection for the whole app" behaviour and do not close what they get back
    - they will leak pool connections until they're updated.
    That update is out of scope for this file;
    it's tracked as the "wrap the 3 handler call sites" step.

Datetime handling - simpler than before:
    The SQLite version registered manual adapters because Python's built-in sqlite3 datetime adapters were deprecated and DATETIME was stored as a TEXT column.
    Neither problem exists here: psycopg v3 converts timezone-aware Python datetimes to/from Postgres TIMESTAMPTZ natively, so there is nothing to register.

Foreign keys and journal mode - not applicable here:
    The SQLite version turned on `PRAGMA foreign_keys` (off by default in SQLite) and WAL mode (for concurrent readers/writers on one file).
    PostgreSQL enforces foreign keys unconditionally and has its own MVCC concurrency model - neither pragma has a Postgres equivalent to set.
"""

import logging
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

# Module-level variable - holds the single Engine instance (and, through it, the connection pool).
# None until init_db() is called.
_engine: Optional[Engine] = None


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------

def init_db(database_url: str) -> Engine:
    """
    Create the SQLAlchemy Engine for the given PostgreSQL connection URL,
    and store it internally so get_connection()/get_readonly_connection() can retrieve it later without needing the URL again.

    This does NOT open a connection itself - the pool connects lazily, on first checkout.
    Safe to call once at startup;
    calling it again replaces the stored engine
    (any connections already checked out from the old one keep working until closed, but new checkouts go through the new engine).

    pool_pre_ping=True: before handing out a pooled connection, SQLAlchemy issues a cheap "is this still alive" check and transparently reconnects if not.
    Worth having for an always-on VPS process
    - a Postgres restart or a network blip between messages shouldn't surface as a mysterious error on whatever handler happens to run next.

    connect_args={"connect_timeout": 5}: without this, a TCP connection attempt to an unreachable Postgres (e.g. Docker not running)
    has no time limit of its own - it hangs until the OS/network stack eventually gives up, which can take a long time and varies by platform.
    Every connection attempt this Engine ever makes - not just the first one - is bounded by this, so a DB that disappears mid-run fails the same way.
    5 seconds is generous for a local/VPS Postgres; callers get a clear error instead of an indefinite hang.

    keepalives_idle/interval/count:
    connect_timeout above only bounds establishing a NEW connection
    - it does nothing for a connection that was already open when Postgres disappeared (e.g. Docker stopped mid-run).
    In that case the OS doesn't get a clean close (no FIN/RST),
    so pool_pre_ping's liveness check just sits there until the OS's own TCP retransmission timeout gives up
    - a couple of minutes by default on both Windows and Linux.
    These settings turn on TCP keepalives and shorten that:
    after 5s idle, probe every 3s, and declare the connection dead after 3 failed probes (5 + 3*3 = 14s worst case) instead of minutes.
    """
    global _engine

    logger.info("Creating database engine")
    _engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "keepalives": 1,
            "keepalives_idle": 5,
            "keepalives_interval": 3,
            "keepalives_count": 3,
        },
    )
    logger.info("Database engine created.")
    return _engine


def check_connection() -> None:
    """
    Verify the database is actually reachable, by checking out a connection and running a trivial query.

    init_db() only builds the Engine - it deliberately does not open a connection (see this function's docstring above),
    so a dead/unreachable Postgres doesn't surface until something tries to actually use it.
    For an always-on process, that "something" was previously the first live message/edit/deletion,
    and the resulting hang there (bounded only as far as connect_timeout now bounds it) looked indistinguishable from the archiver silently doing nothing.

    Callers that want to know immediately - main.py and backfill.py, both right after init_db() - call this instead,
    so a Docker-not-running situation is a clear, fast, startup-time failure rather than a silent one discovered later.
    Raises whatever the underlying connect attempt raises (typically sqlalchemy.exc.OperationalError);
    left uncaught here since "unreachable at startup" should be reported differently by each entry point.
    """
    with get_connection() as conn:
        conn.execute(text("SELECT 1"))


def get_engine() -> Engine:
    """
    Return the active Engine.
    Raises if init_db() hasn't been called yet.
    """
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() before get_engine().")
    return _engine


def close_db() -> None:
    """
    Dispose of the engine's connection pool - closes every pooled connection.
    Should be called on application shutdown.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
        logger.info("Database engine disposed.")


# ---------------------------------------------------------------------------
# Read-write connection (userbot handlers, backfill)
# ---------------------------------------------------------------------------

def get_connection() -> Connection:
    """
    Check out a read-write connection from the pool.

    The caller owns this connection and is responsible for closing it (or using it as a context manager) to return it to the pool
    - see this module's docstring for why that's a real behaviour change from the old single global sqlite3.Connection,
    and which callers haven't been updated for it yet.

    Transactions: SQLAlchemy Core connections use "commit as you go"
    - a transaction begins implicitly on the first statement and stays open until conn.commit() (or conn.rollback()) is called explicitly.
    There is no autocommit-per-statement behaviour to rely on here.
    """
    return get_engine().connect()


# ---------------------------------------------------------------------------
# Read-only connection (API server)
# ---------------------------------------------------------------------------

@contextmanager
def get_readonly_connection() -> Generator[Connection, None, None]:
    """
    Check out a connection from the pool, mark its session read-only, and yield it.
    Always used as a context manager:

        with db.get_readonly_connection() as conn:
            ...

    Replaces the SQLite `file:path?mode=ro` URI trick (api/dependencies.py) - same safeguard in spirit (a write attempted on this connection errors out),
    just enforced by Postgres itself via a session GUC instead of the SQLite driver refusing to open the file for writing.

    Important: `SET default_transaction_read_only` is a SESSION-level setting, not scoped to a single transaction.
    If it weren't reset before the connection goes back into the pool,
    a later get_connection() call from a write-path caller could receive this exact raw connection back from the pool still stuck in read-only mode
    - a write would then fail with an error that has nothing obviously to do with its actual cause.
    The `finally` block below resets it explicitly before closing, so the connection is clean whichever caller the pool hands it to next.
    """
    conn = get_engine().connect()
    conn.execute(text("SET default_transaction_read_only = on"))
    conn.commit()
    try:
        yield conn
    finally:
        # rollback() first, unconditionally:
        # if the caller's code inside the `with` block hit an error - e.g. exactly the "write rejected" error this connection exists to produce
        # - Postgres marks the transaction aborted, and refuses every statement (including our own reset below) until a ROLLBACK happens.
        # Safe to call even when nothing needs rolling back (no-op in that case).
        conn.rollback()
        conn.execute(text("SET default_transaction_read_only = off"))
        conn.commit()
        conn.close()