"""
Manages the connection pool for the CONTROL database (users/invites/refresh_tokens/auth_audit_log).

Deliberately a separate module/pool from db/connection.py, not a second function added there:
    db/connection.py's whole read-write-vs-read-only split exists because the ARCHIVE database has two separate writer/reader processes
    (the userbot writes, the API server only reads) that must not race each other.
    The control database has exactly one process that ever touches it at all - this same API server -
    so there's no reader/writer distinction to enforce here,
    and no second Engine/pool for it would belong bolted onto db/connection.py's archive-specific one.

    This IS the API server writing to a database, which api/server.py's module docstring currently phrases as a blanket rule
    ("never open a write connection here").
    That rule is about the archive DB specifically -
    see this codebase's control_db/schema.py module docstring for the fuller reasoning on why control-DB writes from this process are a deliberate,
    narrow exception rather than a contradiction of it.

Everything below is a trimmed copy of db/connection.py's engine-lifecycle pattern (init_db/get_engine/close_db)
- see that module's docstring for the fuller reasoning on pool_pre_ping, connect_timeout, and keepalives, not repeated here.
The one thing intentionally NOT copied is get_readonly_connection() - there is no read-only use case for this database.
"""

import logging
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None


def init_control_db(database_url: str) -> Engine:
    """
    Create the SQLAlchemy Engine for the control database.
    Mirrors db.connection.init_db() - see that function's docstring for why each create_engine() option is set.
    """
    global _engine

    logger.info("Creating control database engine")
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
    logger.info("Control database engine created.")
    return _engine


def get_engine() -> Engine:
    """Return the active control-DB Engine. Raises if init_control_db() hasn't been called yet."""
    if _engine is None:
        raise RuntimeError("Control database not initialised. Call init_control_db() before get_engine().")
    return _engine


def get_connection() -> Connection:
    """
    Check out a connection from the control-DB pool.

    Read-write, always - unlike db.connection.get_connection(), there's no read-only counterpart here (see this module's docstring for why).
    Caller owns the connection: close it, or use as a context manager.
    """
    return get_engine().connect()


def close_db() -> None:
    """Dispose of the control-DB engine's connection pool. Call on application shutdown."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
        logger.info("Control database engine disposed.")