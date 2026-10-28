"""
Automatic archive-database provisioning:
create a fresh Postgres database and bring it fully up to the current archive schema, for one user - see api/routes/archive.py for the one HTTP caller.

Previously this was a fully manual, two-step process for every account:
an admin (or the person themselves, at a shell) ran `CREATE DATABASE ...` and `alembic upgrade head` against it by hand,
then scripts/manage_admin.py set-archive to point the account at it.
This module does the first two steps programmatically;
set-archive (or api/routes/archive.py calling control_db.queries.set_archive_db_ref directly) still does the third,
deliberately kept as a separate step - see provision_archive_database()'s own docstring for why.

Every archive gets its OWN Postgres role, created here, which owns that one database - see "Isolation" below.

Requires the SAME Postgres role already used for CONTROL_DATABASE_URL to also have the CREATEDB and CREATEROLE privileges (`ALTER ROLE <role> CREATEDB CREATEROLE;`),
which most default local/dev setups already have (a fresh official postgres image's POSTGRES_USER is a superuser)
but a deliberately least-privileged production role likely won't, on purpose.
Missing a privilege is treated as a normal, expected outcome here - see ArchiveProvisioningError's own docstring -
not something this module tries to work around (e.g. by requiring a separate superuser credential no one asked for);
the existing manual set-archive path remains the fallback exactly as it was before this module existed.
(A non-superuser login with exactly those two privileges is covered by the test suite.
One caveat for it: "closing the control database to other roles" below only takes effect when that login OWNS the control database - for a non-owner it is a no-op,
so have the owner run `REVOKE CONNECT ON DATABASE <control> FROM PUBLIC;` once.)

Isolation - why a role per archive
    Without it every archive is reached with the one login in CONTROL_DATABASE_URL,
    so nothing in Postgres stops a connection that was meant for account A from reading account B's messages:
    the only thing keeping them apart would be the application never picking the wrong database name.
    With it, account A's connection authenticates as A's own role, which may open A's database and nothing else:
      - the role OWNS its database (so it can create and migrate its own tables) and has no other privilege: not a superuser, no CREATEDB, no CREATEROLE;
      - CONNECT is revoked from PUBLIC on the archive database (by default Postgres lets every role connect to every database),
      so no other archive's role can even open it;
      - the same is done once for the control database, which holds password hashes and Telegram sessions: an archive role cannot connect to it.
    The role's password is random, shown only here, and stored Fernet-encrypted in the control database (users.archive_db_password) by the caller.
    It is never logged, and never put on a command line or in an exception message: the one statement that carries it goes through the raw psycopg connection,
    because SQLAlchemy would copy the statement text - password included - into any exception it raises.
    Note for operators: Postgres's own server log records that statement if `log_statement` is `ddl` or `all`; keep it at its default (`none`) on a production server.
"""

import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

import psycopg.errors
from psycopg import sql
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

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


# Role and database names are interpolated into DDL (identifiers cannot be bound parameters), so only this shape is ever accepted -
# lowercase letters, digits and underscores, within Postgres's 63-byte identifier limit.
# Everything this module generates already fits; the check is the belt to that braces.
_SAFE_NAME = re.compile(r"^[a-z0-9_]{1,63}$")


def _require_safe_name(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"Refusing to use {name!r} as a database or role name: only a-z, 0-9 and _ (at most 63 characters) are allowed.")
    return name


def _tenant_role_name(db_name: str) -> str:
    """
    The Postgres role that owns archive `db_name`.
    Same string as the database: roles and databases are separate namespaces in Postgres,
    and one name per archive is the least surprising thing to find in `\\du` next to `\\l`.
    """
    return _require_safe_name(db_name)


@dataclass(frozen=True)
class ProvisionedArchive:
    """What provision_archive_database() created: the database, and the login that alone may open it (store the password ENCRYPTED - utils/archive_login.py)."""

    db_name: str
    login: db.ArchiveLogin


def _autocommit_server_connection():
    """A connection on the server's own login with AUTOCOMMIT (CREATE/DROP DATABASE refuse a transaction block).
    Caller closes it."""
    return db.get_server_connection().execution_options(isolation_level="AUTOCOMMIT")


def _permission_denied(what: str) -> ArchiveProvisioningError:
    return ArchiveProvisioningError(
        f"The database role TeleVault connects as doesn't have permission to {what} (it needs the CREATEDB and CREATEROLE privileges).",
        "provisioning_permission_denied",
    )


def _create_or_reset_role(role: str, password: str) -> None:
    """
    CREATE ROLE `role` (LOGIN only, no other privilege) with `password`; if it already exists - the retry of an earlier partial run - give it the new password instead.

    Runs on the RAW psycopg connection with the password composed client-side as a quoted literal: CREATE ROLE cannot take a bound parameter,
    and going through SQLAlchemy would put the full statement text, password included, into any exception it wraps (and so into a log or a traceback).
    psycopg's own errors carry only the server's message.
    The password is token_urlsafe output; the Literal quoting would cope with anything anyway.
    """
    _require_safe_name(role)
    conn = _autocommit_server_connection()
    try:
        raw = conn.connection.driver_connection
        assert raw is not None
        create = sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
        alter = sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
        with raw.cursor() as cur:
            try:
                cur.execute(create)
                logger.info("Created archive role %r", role)
            except psycopg.errors.DuplicateObject:
                cur.execute(alter)
                logger.info("Archive role %r already exists - gave it a new password.", role)
            except psycopg.errors.InsufficientPrivilege as exc:
                raise _permission_denied("create roles") from exc
            # A login that is not a superuser may only hand a database (or table) to a role it is allowed to SET ROLE to:
            # creating the role gives it ADMIN on it, but not that.
            # Without this grant, the CREATE DATABASE ... OWNER / ALTER ... OWNER TO that follow fail with "must be able to SET ROLE".
            # (Postgres 16 syntax - the project's pinned version. For a superuser it is a harmless no-op.)
            # The membership disappears with the role.
            cur.execute(sql.SQL("GRANT {} TO CURRENT_USER WITH SET TRUE").format(sql.Identifier(role)))
    finally:
        conn.close()


def _lock_down_database(db_name: str, owner_role: str) -> None:
    """
    Make `owner_role` the only non-superuser that can open `db_name`: it becomes the owner, and CONNECT is revoked from PUBLIC
    (Postgres grants CONNECT on every new database to every role by default - without this revoke, archive A's role could open archive B).
    Also revokes PUBLIC's CONNECT on the control database, after granting it explicitly to the current login so the application can never lock itself out
    (a no-op, with a warning, for a login that does not own that database).
    Idempotent; safe to repeat.
    """
    _require_safe_name(db_name)
    _require_safe_name(owner_role)
    conn = _autocommit_server_connection()
    try:
        control_db_name = conn.execute(text("SELECT current_database()")).scalar_one()
        try:
            conn.execute(text(f'ALTER DATABASE "{db_name}" OWNER TO "{owner_role}"'))
            conn.execute(text(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM PUBLIC'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{control_db_name}" TO CURRENT_USER'))
            conn.execute(text(f'REVOKE CONNECT ON DATABASE "{control_db_name}" FROM PUBLIC'))
        except ProgrammingError as exc:
            if isinstance(exc.orig, psycopg.errors.InsufficientPrivilege):
                raise _permission_denied("change who owns a database") from exc
            raise
    finally:
        conn.close()


def _create_database_if_missing(db_name: str, owner: str | None = None) -> None:
    """
    CREATE DATABASE db_name, on the same server as the control database (see db.init_server()) - tolerates the database already existing (DuplicateDatabase),
    since that's exactly what a retry after an earlier partial failure (database created, then something failed before archive_db_ref got set) looks like;
    there's no reason to fail a retry just because the first attempt got further than it appeared to.
    `owner` (a role that already exists) becomes the database's owner; on a retry that finds the database already there, _lock_down_database() fixes the owner afterwards.

    CREATE DATABASE cannot run inside a transaction block,
    so this opens its own connection with AUTOCOMMIT rather than reusing whatever connection the caller has open -
    execution_options() changing isolation level on a connection that's already mid-transaction (e.g. from an earlier SELECT on the same connection) would itself raise,
    so this deliberately never shares a connection with anything else.
    """
    conn = _autocommit_server_connection()
    try:
        owner_clause = f' OWNER "{_require_safe_name(owner)}"' if owner else ""
        conn.execute(text(f'CREATE DATABASE "{_require_safe_name(db_name)}"{owner_clause}'))
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


def _migrate_database(db_name: str, login: db.ArchiveLogin | None = None) -> None:
    """
    Run the archive schema's migrations (alembic/versions/) against db_name, exactly the same migration chain a manual `alembic -x url=... upgrade head` runs -
    see this module's own docstring for why this reuses the real migration chain rather than a shortcut like metadata.create_all()
    (consistency: one mechanism that brings any archive database to the current schema, not two that could quietly drift apart as more migrations get added over time).

    Serialized by _provision_lock (see that lock's own comment) around the TELEVAULT_ALEMBIC_URL_OVERRIDE env var -
    the only way to hand alembic/env.py its target database from inside an already-running process.

    `login` is the archive's own role (the migration then runs as the database's owner, so the tables it creates belong to that role);
    None = the server's login, for an archive that has not been secured yet.
    Note the URL, password included, lives in that environment variable for the duration of the upgrade -
    the same exposure the shared password always had here, and it is cleared in the finally block.
    """
    target_url = db.server_database_url(db_name, login)
    # .set() returns a URL object - str(url) masks the password ("***"), which would silently break auth against the new database
    # (a real bug this exact line had until real end-to-end testing against a live Postgres caught it - see this module's own commit notes).
    # render_as_string with hide_password=False is required to get a connection string that actually authenticates.
    cfg = Config(str(_ALEMBIC_INI_PATH))
    # We are inside the running API server: keep ITS logging configuration instead of alembic.ini's (see alembic/env.py for what goes wrong otherwise).
    cfg.attributes["configure_logger"] = False

    with _provision_lock:
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = target_url.render_as_string(hide_password=False)
        try:
            command.upgrade(cfg, "head")
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]

    logger.info("Migrated archive database %r to head.", db_name)


def migrate_archive_database(db_name: str, login: db.ArchiveLogin | None = None) -> None:
    """
    Bring an EXISTING archive database up to the current schema (the same migration chain provisioning runs for a new one), as `login`'s role if given.

    For archives that were created before a release that added a migration: nothing applies those automatically, see RUNBOOK.md ("Updating existing archives").
    Public wrapper so scripts/migrate_archives.py does not reach into this module's private helpers.
    Safe to repeat - only unapplied migrations run.
    """
    _migrate_database(db_name, login)


def head_revision() -> str:
    """The newest archive-schema revision in alembic/versions/ - what a fully migrated archive is at."""
    head = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI_PATH))).get_current_head()
    assert head is not None, "alembic/versions/ has no revisions"
    return head


def archive_revision(db_name: str, login: db.ArchiveLogin | None = None) -> str | None:
    """
    The schema revision the archive database `db_name` is currently at, or None if it has never been migrated at all (no alembic_version row).
    Read-only.
    Raises whatever the connection raises (e.g. OperationalError if the database does not exist) - the caller decides how to report it.
    """
    engine = create_engine(db.server_database_url(db_name, login), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def provision_archive_database(user_id: int) -> ProvisionedArchive:
    """
    Create and fully migrate a new, isolated archive database for user_id: its own role, its own database owned by that role, the whole migration chain run as that role.
    Returns the database name AND the role's login - the caller (api/routes/archive.py) is responsible for saving both on the account
    (control_db.queries.set_archive_db_ref(), with the password encrypted by utils/archive_login.py); this function deliberately doesn't do that itself,
    so it has no control_db dependency at all and stays testable/usable independent of any particular caller's connection-handling.
    The password is generated here and exists nowhere else: if the caller loses it, the archive is unreachable except by the server's own login,
    and a retry simply gives the role a new one (see _create_or_reset_role()).

    Raises ArchiveProvisioningError (see its own docstring) if creation fails for a reason that isn't "already exists" -
    most likely a missing CREATEDB or CREATEROLE privilege.
    Anything else (e.g. the whole Postgres server being unreachable) propagates as whatever SQLAlchemy/psycopg exception it actually is,
    same as every other unhandled database error elsewhere in this codebase.
    """
    db_name = _require_safe_name(_tenant_database_name(user_id))
    role = _tenant_role_name(db_name)
    password = secrets.token_urlsafe(32)
    login = db.ArchiveLogin(role=role, password=password)

    _create_or_reset_role(role, password)
    _create_database_if_missing(db_name, owner=role)
    _lock_down_database(db_name, role)
    _migrate_database(db_name, login)
    verify_archive_login(db_name, login)  # fail here, not on the account's first request, if the new login does not work
    return ProvisionedArchive(db_name=db_name, login=login)


def verify_archive_login(db_name: str, login: db.ArchiveLogin) -> None:
    """
    Prove that `login` really opens `db_name` and is really that role: connect as it and check current_user / current_database().
    Raises on any failure (wrong password, no CONNECT privilege, ...) - never returns a boolean, so a caller cannot forget to look at the answer.
    scripts/secure_archives.py runs this BEFORE storing a password, so the control database never points at a login that does not work.
    """
    engine = create_engine(db.server_database_url(db_name, login), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            user, database = conn.execute(text("SELECT current_user, current_database()")).one()
    finally:
        engine.dispose()
    if (user, database) != (login.role, db_name):
        raise RuntimeError(f"Connected as {user!r} to {database!r}, expected {login.role!r} on {db_name!r}.")


# Moves everything the schema created in an EXISTING archive from the server's login to the archive's own role.
# Needed because that role must own the tables to migrate them, and because Postgres will not let a role that owns nothing read them at all.
# Only the `public` schema is touched, and only what this application created:
# objects belonging to an extension (pg_trgm) and the sequences that belong to a table's column (they follow their table) are skipped.
# Indexes and triggers follow their table.
# Written with quote_ident() and `||` instead of format() so there is no '%' in the statement.
# The role name is validated by _require_safe_name() before it gets here.
_TRANSFER_OWNERSHIP_SQL = """
DO $do$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('e', 'a', 'i'))
  LOOP
    EXECUTE 'ALTER ' || CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END
      || ' public.' || quote_ident(r.relname) || ' OWNER TO ' || quote_ident('__ROLE__');
  END LOOP;
  FOR r IN
    SELECT p.oid::regprocedure::text AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE 'ALTER FUNCTION ' || r.signature || ' OWNER TO ' || quote_ident('__ROLE__');
  END LOOP;
END
$do$
"""


def _transfer_object_ownership(db_name: str, role: str) -> None:
    """Make `role` the owner of every table, view, sequence and function the application created in `db_name` (see _TRANSFER_OWNERSHIP_SQL).
    One transaction: all or nothing."""
    engine = create_engine(db.server_database_url(db_name), poolclass=NullPool)  # the server's login: it still owns everything at this point
    try:
        with engine.begin() as conn:
            conn.execute(text(_TRANSFER_OWNERSHIP_SQL.replace("__ROLE__", _require_safe_name(role))))
    finally:
        engine.dispose()


def secure_archive_database(db_name: str) -> db.ArchiveLogin:
    """
    Give an archive that was created with the server's shared login (before per-archive roles) its own role, exactly as provisioning would have:
    create the role, make it the database's owner, close the database to every other role, hand it the tables, and PROVE the new login works.
    Returns the login; the caller (scripts/secure_archives.py) stores its encrypted password on the account.

    Safe to repeat: a retry after a partial failure gives the role a new password and redoes the (idempotent) rest.
    The database's data is never read or changed - only who owns it.
    Until the caller has stored the new login the application keeps using the server's login, which still works for a superuser.
    (A non-superuser server login loses access to the tables at the ownership step, so for such a setup run the script while the API and workers are stopped.)

    Raises ArchiveProvisioningError for a missing privilege,
    ValueError for a database name that is not a plain a-z/0-9/_ identifier (rename it or leave that archive as it is).
    """
    role = _tenant_role_name(db_name)
    conn = _autocommit_server_connection()
    try:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}).first() is not None
    finally:
        conn.close()
    if not exists:  # check BEFORE creating anything: a role for a database that is not there would just be left behind
        raise ArchiveProvisioningError(
            "the database does not exist (users.archive_db_ref points at a database that was never created, or it was dropped)",
            "archive_database_missing",
        )
    password = secrets.token_urlsafe(32)
    login = db.ArchiveLogin(role=role, password=password)

    _create_or_reset_role(role, password)
    _lock_down_database(db_name, role)
    _transfer_object_ownership(db_name, role)
    verify_archive_login(db_name, login)
    return login
