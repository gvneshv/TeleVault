"""
Automatic archive-database provisioning:
create a fresh Postgres database and bring it fully up to the current archive schema, for one user - see api/routes/archive.py for the one HTTP caller.

Previously this was a fully manual, two-step process for every account:
an admin (or the person themselves, at a shell) ran `CREATE DATABASE ...` and `alembic upgrade head` against it by hand,
then scripts/manage_admin.py set-archive to point the account at it.
This module does the first two steps programmatically;
set-archive (or api/routes/archive.py calling control_db.queries.set_archive_db_ref directly) still does the third,
deliberately kept as a separate step - see provision_archive_database()'s own docstring for why.

Requires the SAME Postgres role already used for DATABASE_URL/CONTROL_DATABASE_URL to also have the CREATEDB privilege (`ALTER ROLE <role> CREATEDB;`),
which most default local/dev setups already have (a fresh official postgres image's POSTGRES_USER is a superuser)
but a deliberately least-privileged production role likely won't, on purpose.
Missing that privilege is treated as a normal, expected outcome here - see ArchiveProvisioningError's own docstring -
not something this module tries to work around (e.g. by requiring a separate superuser credential no one asked for);
the existing manual set-archive path remains the fallback exactly as it was before this module existed.
"""

import logging
import os
import threading
from pathlib import Path

import psycopg.errors
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

import db

logger = logging.getLogger(__name__)

# Guards the whole create-then-migrate sequence, including the brief window where TELEVAULT_ALEMBIC_URL_OVERRIDE is set in os.environ (see alembic/env.py) -
# that's process-wide mutable state, and archive.py's route is a plain `def` (no `await` in its body),
# which Starlette runs via a real thread pool, not the single-threaded event loop.
# Without this lock, two concurrent provisioning requests (different users, doesn't matter) could genuinely interleave and one could migrate against the OTHER's target URL.
# A single global lock is fine here - provisioning is rare (once per new account) and briefly serializing it costs nothing anyone will notice.
_provision_lock = threading.Lock()

# Repo root, computed the same way alembic/env.py computes it -
# this module isn't guaranteed to run with the repo root as CWD any more than env.py is (see that file's own comment on this).
_ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"


class ArchiveProvisioningError(Exception):
    """
    Raised when automatic provisioning can't proceed - NOT for "it worked but the archive is empty" (that's just normal), only for a real failure.
    `reason` matches the {"message", "reason"} shape api/routes/archive.py wraps this in for the HTTP response - see web/js/lib/errors.js's describeError().
    Always leaves control_db.users.archive_db_ref untouched;
    the caller decides whether/how to fall back (today: the existing manual set-archive script still works regardless of why this raised).
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _tenant_database_name(user_id: int) -> str:
    """
    The archive_db_ref this user will get.
    Derived from user_id (a Postgres bigserial, so guaranteed unique and already a safe identifier) rather than username -
    a username can contain characters that aren't valid in a Postgres identifier without quoting games,
    and could theoretically change later (nothing today allows renaming a user, but there's no reason to build in that fragility).
    """
    return f"televault_archive_{user_id}"


def _create_database_if_missing(db_name: str) -> None:
    """
    CREATE DATABASE db_name, on the same server as the primary DATABASE_URL - tolerates the database already existing (DuplicateDatabase),
    since that's exactly what a retry after an earlier partial failure (database created, then something failed before archive_db_ref got set) looks like;
    there's no reason to fail a retry just because the first attempt got further than it appeared to.

    CREATE DATABASE cannot run inside a transaction block,
    so this opens its own connection with AUTOCOMMIT rather than reusing whatever connection the caller has open -
    execution_options() changing isolation level on a connection that's already mid-transaction (e.g. from an earlier SELECT on the same connection) would itself raise,
    so this deliberately never shares a connection with anything else.
    """
    conn = db.get_connection().execution_options(isolation_level="AUTOCOMMIT")
    try:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        logger.info("Created archive database %r", db_name)
    except ProgrammingError as exc:
        if isinstance(exc.orig, psycopg.errors.DuplicateDatabase):
            logger.info("Archive database %r already exists - reusing it.", db_name)
        elif isinstance(exc.orig, psycopg.errors.InsufficientPrivilege):
            raise ArchiveProvisioningError(
                "The database role TeleVault connects as doesn't have permission to create databases.",
                "provisioning_permission_denied",
            ) from exc
        else:
            raise
    finally:
        conn.close()


def _migrate_database(db_name: str) -> None:
    """
    Run the archive schema's migrations (alembic/versions/) against db_name, exactly the same migration chain `alembic upgrade head` runs against DATABASE_URL -
    see this module's own docstring for why this reuses the real migration chain rather than a shortcut like metadata.create_all()
    (consistency: one mechanism that brings any archive database to the current schema, not two that could quietly drift apart as more migrations get added over time).

    Serialized by _provision_lock (see that lock's own comment) around the TELEVAULT_ALEMBIC_URL_OVERRIDE env var -
    the only way to hand alembic/env.py a target database other than settings.database_url from inside an already-running process.
    """
    target_url = make_url(db.get_engine().url).set(database=db_name)
    # .set() returns a URL object - str(url) masks the password ("***"), which would silently break auth against the new database
    # (a real bug this exact line had until real end-to-end testing against a live Postgres caught it - see this module's own commit notes).
    # render_as_string with hide_password=False is required to get a connection string that actually authenticates.
    cfg = Config(str(_ALEMBIC_INI_PATH))

    with _provision_lock:
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = target_url.render_as_string(hide_password=False)
        try:
            command.upgrade(cfg, "head")
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]

    logger.info("Migrated archive database %r to head.", db_name)


def provision_archive_database(user_id: int) -> str:
    """
    Create and fully migrate a new archive database for user_id.
    Returns the database name - the caller (api/routes/archive.py) is responsible for saving it as this user's archive_db_ref via control_db.queries.set_archive_db_ref();
    this function deliberately doesn't do that itself,
    so it has no control_db dependency at all and stays testable/usable independent of any particular caller's connection-handling.

    Raises ArchiveProvisioningError (see its own docstring) if creation fails for a reason that isn't "already exists" - most likely a missing CREATEDB privilege.
    Anything else (e.g. the whole Postgres server being unreachable) propagates as whatever SQLAlchemy/psycopg exception it actually is,
    same as every other unhandled database error elsewhere in this codebase.
    """
    db_name = _tenant_database_name(user_id)
    _create_database_if_missing(db_name)
    _migrate_database(db_name)
    return db_name