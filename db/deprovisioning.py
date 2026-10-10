"""
Archive-database teardown: the other half of db/provisioning.py, for account deletion.

Only caller: api/routes/admin.py's DELETE /admin/users/{id}, and scripts/manage_admin.py's delete-admin subcommand -
both AFTER control_db.queries.delete_user_completely() has already committed removing the users row and everything that pointed at it.
That ordering matters and is deliberate: if this ran first and then the control_db delete failed for any reason,
the account would be left pointing at an archive_db_ref that no longer exists -
exactly the "misconfigured" state db.is_missing_database_error() exists to detect elsewhere - for an account that's supposedly still there.
Dropping the archive LAST means the only possible failure mode is the opposite:
control_db says the account is gone, but the now-orphaned database briefly still physically exists (until this call, or a retry of it, completes) -
a leaked database rather than a dangling reference, and the strictly safer of the two failure modes to be left in.

See db/provisioning.py's own module docstring for the CREATE DATABASE side of this and why both live in db/ rather than control_db/:
this operates on the ARCHIVE server/role, not the control database, and (like provisioning) has no control_db dependency at all.
"""

import logging

from sqlalchemy import text

import db

logger = logging.getLogger(__name__)


def drop_archive_database(db_name: str) -> None:
    """
    Permanently and irreversibly DROP a user's archive database, tolerating it already being gone (DROP DATABASE IF EXISTS -
    the same "a retry after an earlier partial failure looks like this" tolerance db.provisioning._create_database_if_missing() already applies to CREATE DATABASE,
    mirrored here for the opposite direction:
    a retried delete after the database was already dropped but something else in the caller's own cleanup failed is not an error).

    Disposes this process's own cached connection pool to db_name FIRST (db.dispose_tenant_engine()) - Postgres refuses to drop a database with active connections,
    and get_archive_connection() (api/dependencies.py) is exactly the kind of thing that would have left one pooled here if this account was ever actually used.
    Does NOT use `WITH (FORCE)` (Postgres 13+ only, forcibly disconnects OTHER sessions too) to handle any remaining external connection:
    every connection to a per-user archive database in this app's own design is either this pooled one (just disposed)
    or a short-lived request-scoped one that's already closed by the time a request finishes -
    there's no legitimate persistent external connection FORCE would be needed to evict,
    and reaching for it anyway would silently paper over a real bug (something holding a connection open that shouldn't be) instead of surfacing it.

    Runs in AUTOCOMMIT, on its own dedicated connection - same requirement, same reason, as db.provisioning._create_database_if_missing():
    DROP DATABASE cannot run inside a transaction block at all.

    Raises whatever the underlying exception actually is for anything other than "already doesn't exist"
    (e.g. ObjectInUse, if some other, truly external connection is somehow still attached) - same "propagate,
    don't guess at a recovery" posture as every other unhandled database error in this codebase.
    """
    db.dispose_tenant_engine(db_name)
    conn = db.get_server_connection().execution_options(isolation_level="AUTOCOMMIT")
    try:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        logger.info("Dropped archive database %r.", db_name)
    finally:
        conn.close()