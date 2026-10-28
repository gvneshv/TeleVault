"""
Bring every account's archive database up to the current schema - the one command to run after pulling an update that added an archive migration.

Why this exists
    Each account's archive is its own Postgres database, created (and migrated) when the account set it up. A later release can add a migration to the archive schema
    (alembic/versions/), but nothing applies it to archives that already exist - only a newly created archive gets the whole chain.
    Running `alembic -x url=...` by hand for each one works in principle, but needs a connection URL with a password, and there is no per-account password to look up:
    Every archive lives on the same Postgres server, and the password for it is stored (encrypted) in the control database, not anywhere you would have to look it up:
    an archive that has its own role (everything created after per-archive logins, and anything scripts/secure_archives.py has processed) is migrated as that role;
    an older one is reached with the server's login from CONTROL_DATABASE_URL, as before.
    This script reads CONTROL_DATABASE_URL and FERNET_KEY from your .env like the rest of the app, asks the control database which archives exist, and migrates each one.
    Nothing to look up, nothing to type, and no password ever appears on a command line (where `ps` and shell history would show it) or in this script's output.

Usage (from the repo root, with the same .env the API server uses; the virtualenv active):
    python scripts/migrate_archives.py --dry-run     # only report which archives are out of date
    python scripts/migrate_archives.py               # migrate every archive that is

Safe to repeat, and safe to run while the API and archivers are running: it only applies unapplied migrations, and the ones shipped so far only ADD tables, which
does not block or disturb a live archiver. An archive that is already current is left untouched. One archive failing (say, its database was removed by hand) is reported
and does not stop the others; the exit status is then 1.

The control database itself is migrated separately (`alembic -c alembic_control.ini upgrade head`) - this script only touches archives.
"""

import argparse
import logging
import sys
from pathlib import Path

# This script lives in scripts/, one level below the repo root - same sys.path fix as scripts/manage_admin.py, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_db
import db
from config import settings
from control_db import queries as cq
from db import provisioning
from utils.archive_login import archive_login_from_row


def _describe_failure(exc: Exception) -> str:
    """
    One short line for a failed archive: the exception class and, for the common cases, a plain reason.
    Never the connection URL, and never a traceback - the person running this needs to know WHICH archive and roughly why, not read a stack.
    """
    from sqlalchemy.exc import OperationalError

    if isinstance(exc, OperationalError):
        if db.is_missing_database_error(exc):
            return "the database does not exist (users.archive_db_ref points at a database that was never created, or it was dropped)"
        return "could not connect to the database (is PostgreSQL running?)"
    first_line = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first_line}"[:300]


def migrate_all(dry_run: bool) -> int:
    """
    Visit every archive and bring it to head (or, with dry_run, just report). Returns the process exit status: 0 if every archive is current afterwards
    (or, in a dry run, nothing failed to be inspected), 1 if any archive could not be inspected or migrated.
    """
    control_db.init_control_db(settings.control_database_url)
    # Every archive is on the control database's server with the same credentials - see this module's docstring.
    db.init_server(settings.control_database_url)
    try:
        with control_db.get_connection() as conn:
            archives = cq.list_archive_databases(conn)
        if not archives:
            print("No account has an archive database yet - nothing to do.")
            return 0

        head = provisioning.head_revision()
        failed = outdated = migrated = 0
        for archive in archives:
            label = f"{archive['username']} (account {archive['id']}, database {archive['archive_db_ref']})"
            try:
                with control_db.get_connection() as conn:
                    login = archive_login_from_row(cq.get_archive_login_row(conn, archive["id"]) or {})  # None = not secured yet: the server's login
                current = provisioning.archive_revision(archive["archive_db_ref"], login)
                if current == head:
                    print(f"  ok       {label}: already up to date")
                elif dry_run:
                    outdated += 1
                    print(f"  OUTDATED {label}: at {current or 'no revision'}, newest is {head}")
                else:
                    provisioning.migrate_archive_database(archive["archive_db_ref"], login)
                    migrated += 1
                    print(f"  migrated {label}: {current or 'no revision'} -> {head}")
            except Exception as exc:  # noqa: BLE001 - one broken archive must not stop the rest
                failed += 1
                print(f"  FAILED   {label}: {_describe_failure(exc)}")

        if dry_run:
            print(f"{len(archives)} archive(s) checked: {outdated} out of date, {failed} could not be checked. Nothing was changed.")
        else:
            print(f"{len(archives)} archive(s) checked: {migrated} migrated, {failed} failed.")
        return 1 if failed else 0
    finally:
        db.close_tenant_engines()
        db.close_server()
        control_db.close_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Only report which archives are out of date; change nothing.")
    args = parser.parse_args(argv)

    # Quiet by default: alembic's own INFO lines would bury the one-line-per-archive report above. Real problems still show.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return migrate_all(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
