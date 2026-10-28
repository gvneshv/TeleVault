"""
Give every existing archive its OWN Postgres login - the one-time upgrade for archives created before per-archive logins existed.

Why
    Until now every archive was reached with the single login in CONTROL_DATABASE_URL, so Postgres itself could not tell one account's archive from another's:
    only the application's care kept them apart.
    An archive that has its own role can be opened by that role alone (db/provisioning.py explains the mechanism).
    Archives created from now on get a role automatically; this script does the same for the ones that already exist.

What it does, per archive that has no login of its own yet
    1. creates a role named after the database (no privilege except logging in), with a random password;
    2. makes that role the database's owner and closes the database to every other role;
    3. hands the role the tables, so it can read, write and migrate them (the data itself is not touched);
    4. connects as the new role to PROVE it works - and only then stores the password, encrypted with your FERNET_KEY, on the account in the control database.
    If anything fails for one archive, it says which and carries on; that archive simply keeps working with the shared login until you run the script again.

Usage (from the repo root, with the same .env the API server uses - it needs CONTROL_DATABASE_URL and FERNET_KEY; the virtualenv active):
    python scripts/secure_archives.py --dry-run     # only list which archives are not secured yet
    python scripts/secure_archives.py               # secure them
    python scripts/secure_archives.py --repair      # ALSO give a new password to archives whose stored login no longer works

Safe to repeat.
An archive that already has a login is checked (the stored password must still open it) and otherwise left alone - unless it does not open any more
(typically FERNET_KEY was lost or changed, so the stored passwords cannot be decrypted): that is reported as FAILED, and `--repair` replaces such a login with a fresh one. No password is ever printed, passed on a command line or written to a log.
After it has run, RESTART the archiver and backfill workers: a running one keeps the shared login it started with until it is restarted
(the API server picks up the new login by itself on the next request).
Needs the same Postgres privileges as creating an archive (CREATEDB and CREATEROLE, or a superuser).
Do not lose or change FERNET_KEY afterwards: it is what the stored passwords are encrypted with, same as the Telegram sessions.
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
from utils.archive_login import archive_login_from_row, encrypt_archive_password


def _describe_failure(exc: Exception) -> str:
    """One short line for a failed archive: never the connection URL, a password or a traceback."""
    from sqlalchemy.exc import OperationalError

    if isinstance(exc, provisioning.ArchiveProvisioningError):
        return str(exc)
    if isinstance(exc, OperationalError):
        if db.is_missing_database_error(exc):
            return "the database does not exist (users.archive_db_ref points at a database that was never created, or it was dropped)"
        return "could not connect (is PostgreSQL running? did the stored password stop working?)"
    first_line = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first_line}"[:300]


def secure_all(dry_run: bool, repair: bool = False) -> int:
    """
    Visit every archive and secure those without their own login (or, with dry_run, only report); with `repair`, also replace a stored login that no longer works.
    Returns the exit status: 0 if every archive has a working login afterwards (dry run: nothing failed to be checked), 1 if any archive failed.
    """
    control_db.init_control_db(settings.control_database_url)
    db.init_server(settings.control_database_url)
    try:
        with control_db.get_connection() as conn:
            archives = cq.list_archive_databases(conn)
        if not archives:
            print("No account has an archive database yet - nothing to do.")
            return 0

        failed = shared = secured = 0
        for archive in archives:
            label = f"{archive['username']} (account {archive['id']}, database {archive['archive_db_ref']})"
            db_name = archive["archive_db_ref"]
            try:
                broken = False
                if archive["archive_db_user"] is not None:
                    try:
                        with control_db.get_connection() as conn:
                            login = archive_login_from_row(cq.get_archive_login_row(conn, archive["id"]) or {})
                        assert login is not None
                        provisioning.verify_archive_login(db_name, login)
                        print(f"  ok       {label}: has its own login")
                        continue
                    except Exception:
                        if not repair:
                            raise
                        broken = True  # --repair: the stored login is unusable, so treat the archive as if it had none
                if dry_run:
                    shared += 1
                    print(f"  UNUSABLE {label}: the stored login does not work" if broken else f"  SHARED   {label}: still uses the shared login")
                else:
                    login = provisioning.secure_archive_database(db_name)
                    with control_db.get_connection() as conn:
                        stored = cq.set_archive_db_login(conn, archive["id"], login.role, encrypt_archive_password(login.password))
                    if not stored:  # the account lost its archive_db_ref while we worked - nothing was recorded, the role is harmless
                        raise RuntimeError("the account no longer has this archive; nothing was stored")
                    db.dispose_tenant_engine(db_name)  # drop any pool that still authenticates as the shared login
                    secured += 1
                    print(f"  secured  {label}: now uses its own login")
            except Exception as exc:  # noqa: BLE001 - one broken archive must not stop the rest
                failed += 1
                print(f"  FAILED   {label}: {_describe_failure(exc)}")

        if dry_run:
            print(f"{len(archives)} archive(s) checked: {shared} still on the shared login, {failed} could not be checked. Nothing was changed.")
        else:
            print(f"{len(archives)} archive(s) checked: {secured} secured, {failed} failed.")
            if secured:
                print("Restart the archiver and backfill workers so they pick up the new logins.")
        return 1 if failed else 0
    finally:
        db.close_tenant_engines()
        db.close_server()
        control_db.close_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Only report which archives still use the shared login; change nothing.")
    parser.add_argument("--repair", action="store_true", help="Also give a new password to archives whose stored login no longer works (e.g. FERNET_KEY was lost).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return secure_all(args.dry_run, args.repair)


if __name__ == "__main__":
    sys.exit(main())
