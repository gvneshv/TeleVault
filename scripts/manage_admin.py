"""
Create or delete the admin account, and manage invites, on the control database - outside the normal HTTP API.

Why this has to exist at all:
    control_db.schema.invites.created_by is NOT NULL with a foreign key to users.id,
    and admin-only account management lives behind api.dependencies.require_admin (api/routes/admin.py) - every ordinary account is created FROM an invite,
    and every invite is created BY the admin.
    That's watertight once the admin exists, but it means the admin can never come into existence through POST /auth/register at all, in the ordinary way:
    there is no admin yet to have created their invite.
    Something outside that loop has to insert the first row directly.

    Two ways to do that now exist side by side:
      - config.Settings.bootstrap_admin_token (an operator-chosen phrase in .env)
        lets the very first person register through the ORDINARY web UI/POST /auth/register and become admin automatically - see that setting's own docstring.
        This is the RECOMMENDED path for a fresh instance: no shell access needed at all,
        and it's what everything past user #1 (invites, login) was already designed around.
      - `create` below is the original, shell-access answer, kept as a fallback for whoever prefers it,
        or needs it (e.g. TELEVAULT_BOOTSTRAP_ADMIN_TOKEN was never configured, or the admin account was removed via `delete-admin` below and needs recreating).
    Both are subject to the SAME single-admin invariant (Decisions Log: TeleVault has exactly one admin,
    full stop - control_db.schema.ix_users_single_admin enforces this at the database level) -
    `create` below checks control_db.queries.admin_exists() itself and refuses if an admin already exists, same as the bootstrap-token branch of register() does.

    There is deliberately no `promote` subcommand any more (an earlier version of this script had one).
    Once an instance is single-admin, "promote a second user to admin" is not a smaller version of that feature to keep around,
    it's the exact thing the model rules out -
    keeping a disabled-by-the-database code path around to grant a role the schema will then simply refuse would only invite someone to wonder why it failed,
    rather than the option not existing in the first place.

    This script's other two subcommands exist for the SAME "no HTTP endpoint should ever do this" reason as each other, not because they're related in what they do:
      - `delete-admin` - the sole admin removing THEIR OWN account. control_db.queries.delete_user_completely()
        (also used by DELETE /admin/users/{id} for ordinary users) has no is_admin special-casing in itself -
        the restriction is enforced entirely by WHICH callers are allowed to reach it, and this script,
        run at the shell by whoever already has that access, is the only one allowed to reach it for an admin target.
        There is no path from the web UI or API to this outcome, for any account, ever - not a check that could be bypassed, a door that was never built.
      - `create-invite` / `set-archive` also have HTTP equivalents now (POST /admin/invites and the manual provisioning fallback respectively -
        see api/routes/admin.py's own module docstring for exactly which of scripts/manage_admin.py's original capabilities moved to the API and which stayed CLI-only)
        but are kept here too, as a recovery path for an admin who's locked out of the UI/API for some other reason.

Safety boundaries this script holds itself to (read before extending it):
    - Never touches telegram_api_id / telegram_api_hash / telegram_session_string for ANY user, in any subcommand except set-archive's own archive_db_ref write.
      `create` and `delete-admin` call control_db.queries functions (create_admin_user / delete_user_completely)
      that never read or alter those columns except as part of a full-account delete - see those functions' own docstrings in control_db/queries.py.
    - Password is NEVER accepted as a command-line argument.
      A --password flag would land in shell history (~/.bash_history)
      and be visible to any other process on the same machine for as long as this one runs (`ps aux` shows full argv) - both are real, not hypothetical,
      exposures for a value that's about to become someone's login credential.
      getpass.getpass() is used instead: it reads from the terminal with input echo disabled and never touches argv, shell history,
      or (unless something in the environment is very unusually configured) any log file this process might write.
    - Password is never printed, logged, or included in any error message - only the fact that one was or wasn't provided/matched.
    - `delete-admin` requires typing the target's EXACT username back, not just a y/N -
      see cmd_delete_admin()'s own docstring for why this one subcommand gets a stricter confirmation than everything else here.
    - Every subcommand that writes anything requires an interactive confirmation first (y/N, or - delete-admin only - the username itself).
      There is no --yes/--force flag - these are rare, high-privilege operations run by hand,
      not something meant to be scripted/automated, so there's no legitimate case for skipping it.
    - Runs directly against control_database_url using this repo's own connection/query modules
      (control_db.connection, control_db.queries) - no ad-hoc SQL lives in this file.
      Any future change to what "safe to touch" means only has to be made once, in control_db/queries.py, not duplicated here.

Usage (run on the server, by whoever already has Postgres/VPS access - this is a local CLI tool, never reachable over HTTP):
    python scripts/manage_admin.py create --username alice
    python scripts/manage_admin.py delete-admin --username alice
    python scripts/manage_admin.py set-archive --username alice --db-name alice_archive
    python scripts/manage_admin.py create-invite --created-by alice
    python scripts/manage_admin.py create-invite --created-by alice --expires-hours 48

`create` prompts for the new password twice (entry + confirmation),
same 8-character minimum as POST /auth/register (api/schemas/auth.py) -
there's no reason a script-created admin account should be allowed to be weaker than one created through the normal flow.
Refuses outright (before even prompting for a password) if an admin already exists - see control_db.queries.admin_exists().

After `create`: the account has no Telegram credentials linked yet, and no archive_db_ref either - run `set-archive`
(against a database name you've already created and migrated by hand with `alembic upgrade head` pointed at it)
before that account can use /api/chats, /messages, /deleted, or /stats,
since get_archive_connection() (api/dependencies.py) 409s on a NULL archive_db_ref.

`delete-admin` is IRREVERSIBLE: the account row, its archive database (if any, dropped entirely - every message in it),
and every Telegram credential/session it had linked are all gone for good;
the username becomes available for reuse from a clean slate.
See control_db.queries.delete_user_completely()'s and db.deprovisioning.drop_archive_database()'s own docstrings for exactly what that does and doesn't touch.
Refuses if the admin has created any invites that still exist (control_db.queries.delete_user_completely() raises ValueError for that) -
resolve those by hand (delete the rows, or wait for them to expire and clean them up) before retrying.

`create-invite` refuses to run if --created-by doesn't resolve to an admin.
Default expiry is 24 hours (--expires-hours to change it);
the printed token is the whole invite - hand it to the invitee directly (chat, in person, however you'd share a one-time code),
since anyone who has it can register with it before it expires or you revoke it
(there's no revoke command yet either - delete the row by hand if you need to invalidate one early).
"""

import argparse
import getpass
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# This script lives in scripts/, one level below the repo root -
# add the repo root to sys.path explicitly so these imports resolve regardless of the working directory it's invoked from
# (same fix scripts/migrate_sqlite_to_postgres.py and alembic/env.py needed, for the same reason).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_db
from api.process_utils import running_process_reason
from config import settings
from control_db import queries as cq
from db.deprovisioning import drop_archive_database
from sqlalchemy.exc import IntegrityError
from utils.security import hash_password

MIN_PASSWORD_LENGTH = 8  # matches RegisterIn.password in api/schemas/auth.py


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def _prompt_new_password() -> str:
    """
    Prompt for a new password twice and confirm they match, never accepting one from argv.
    Exits (not raises) on mismatch/too-short so the caller doesn't have to distinguish "user cancelled" from "unexpected exception" -
    both just mean "nothing was written".
    """
    password = getpass.getpass("New password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        sys.exit("Passwords did not match. Nothing was created.")
    return password


def cmd_create(username: str) -> None:
    conn = control_db.get_connection()
    try:
        # Checked BEFORE even asking for a username collision or a password - see this module's own docstring for the single-admin invariant this is upholding;
        # there's no point walking someone through two password prompts only to fail at the very last INTEGRITY-checked step.
        if cq.admin_exists(conn):
            sys.exit(
                "An admin account already exists. TeleVault only ever has one - see this script's own module "
                "docstring. Use 'delete-admin' first if you genuinely need to replace it."
            )

        if cq.get_user_by_username(conn, username) is not None:
            sys.exit(f"A user named '{username}' already exists.")

        password = _prompt_new_password()

        print(f"\nAbout to create a NEW ADMIN user '{username}'.")
        if not _confirm("Proceed?"):
            print("Cancelled. Nothing was written.")
            return

        password_hash = hash_password(password)
        try:
            user_id = cq.create_admin_user(conn, username, password_hash)
        except IntegrityError:
            # conn.rollback() already happened inside create_admin_user() before it re-raised.
            # Same race as _register_bootstrap_admin() (api/routes/auth.py) can hit against the bootstrap-token path -
            # genuinely unlikely for two operators to be running this at once, but re-checking rather than assuming which of the two possible causes it was.
            if cq.admin_exists(conn):
                sys.exit("An admin account was created concurrently (by another process). Nothing was written here.")
            sys.exit(f"A user named '{username}' already exists (created concurrently). Nothing was written.")

        print(f"Created admin user '{username}' (id={user_id}).")
        print("This account has no Telegram credentials linked yet - complete that via the normal")
        print("POST /telegram/credentials + /telegram/link/send-code + /telegram/link/confirm flow after logging in,")
        print("then POST /archive/provision to create and migrate this account's own archive database.")
    finally:
        conn.close()


def cmd_delete_admin(user_id: int | None, username: str | None) -> None:
    """
    Irreversibly delete the ONE admin account.
    See this module's own docstring for why this is the only place this action can happen at all - not gated behind a permission check reachable some other way,
    simply never exposed anywhere else.

    Requires typing the target's exact username back (not just y/N, unlike every other confirmation in this script) -
    deleting the sole admin is qualitatively different from every other action here: it can strand the whole instance
    (no one left who can create invites, lock/unlock anyone, or run this script's OTHER subcommands against a different target)
    if run against the wrong account by mistake, e.g. a fumbled --id.
    A single keystroke answering 'y' out of habit is exactly the failure mode this is meant to catch;
    retyping the name forces the operator to actually look at what they're about to remove.
    """
    conn = control_db.get_connection()
    try:
        user = cq.get_user_by_id(conn, user_id) if user_id is not None else cq.get_user_by_username(conn, username)
        if user is None:
            identifier = f"id={user_id}" if user_id is not None else f"username='{username}'"
            sys.exit(f"No user found with {identifier}. Nothing was written.")
        if not user["is_admin"]:
            sys.exit(f"'{user['username']}' (id={user['id']}) isn't an admin - this subcommand is for the admin account only.")

        # Same instance-wide check api/routes/telegram.py's unlink() and api/routes/admin.py's delete_user() both make before their own destructive action -
        # see api.process_utils.running_process_reason()'s own docstring for why this doesn't only check whether the archiver is running for THIS account.
        reason = running_process_reason(settings.heartbeat_path, settings.backfill_status_path)
        if reason is not None:
            sys.exit(
                f"Refusing to delete: {'the userbot is running' if reason == 'already_running' else 'a backfill is running'}. "
                "Stop it first (POST /api/telethon/stop or /api/backfill/stop) and try again."
            )

        print(f"\nThis will PERMANENTLY delete admin '{user['username']}' (id={user['id']}):")
        print("  - the account itself (the username becomes available again, from a clean slate)")
        if user["archive_db_ref"]:
            print(f"  - its entire archive database ('{user['archive_db_ref']}') and every message in it")
        if user["telegram_session_string"] or user["telegram_api_id"]:
            print("  - its linked Telegram credentials and session")
        print("This cannot be undone.")
        typed = input(f"Type '{user['username']}' to confirm: ").strip()
        if typed != user["username"]:
            print("Did not match. Cancelled. Nothing was written.")
            return

        try:
            archive_db_ref = cq.delete_user_completely(conn, user["id"], actor_id=user["id"])
        except ValueError as exc:
            sys.exit(str(exc))
        if archive_db_ref is not None:
            drop_archive_database(archive_db_ref)

        print(f"'{user['username']}' (id={user['id']}) and everything belonging to it has been deleted.")
    finally:
        conn.close()


def cmd_set_archive(user_id: int | None, username: str | None, db_name: str) -> None:
    conn = control_db.get_connection()
    try:
        user = cq.get_user_by_id(conn, user_id) if user_id is not None else cq.get_user_by_username(conn, username)
        if user is None:
            identifier = f"id={user_id}" if user_id is not None else f"username='{username}'"
            sys.exit(f"No user found with {identifier}. Nothing was written.")

        print(f"\nAbout to set archive_db_ref for '{user['username']}' (id={user['id']}) to '{db_name}'.")
        if user["archive_db_ref"] is not None:
            print(f"This OVERWRITES the existing value: '{user['archive_db_ref']}'.")
        print("This only records the reference - it does NOT create or migrate the database.")
        print(f"Make sure '{db_name}' already exists and has had `alembic upgrade head` run against it.")
        if not _confirm("Proceed?"):
            print("Cancelled. Nothing was written.")
            return

        cq.set_archive_db_ref(conn, user["id"], db_name)
        print(f"'{user['username']}' (id={user['id']}) now points at archive database '{db_name}'.")
    finally:
        conn.close()


def cmd_create_invite(user_id: int | None, username: str | None, expires_hours: int) -> None:
    conn = control_db.get_connection()
    try:
        creator = cq.get_user_by_id(conn, user_id) if user_id is not None else cq.get_user_by_username(conn, username)
        if creator is None:
            identifier = f"id={user_id}" if user_id is not None else f"username='{username}'"
            sys.exit(f"No user found with {identifier}. Nothing was written.")

        if not creator["is_admin"]:
            sys.exit(f"'{creator['username']}' (id={creator['id']}) isn't an admin. Invite creation is an admin-only action.")

        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        print(f"\nAbout to create an invite as '{creator['username']}' (id={creator['id']}), expiring in {expires_hours}h ({expires_at.isoformat()}).")
        if not _confirm("Proceed?"):
            print("Cancelled. Nothing was written.")
            return

        token = cq.create_invite(conn, creator["id"], expires_at)
        print(f"\nInvite token: {token}")
        print(f"Expires: {expires_at.isoformat()}")
        print("Hand this to the invitee directly - anyone who has it can register with it before it expires.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_create = subparsers.add_parser("create", help="Create the admin user (refuses if one already exists).")
    p_create.add_argument("--username", required=True)

    p_delete_admin = subparsers.add_parser("delete-admin", help="Irreversibly delete the admin account (never exposed over HTTP - see module docstring).")
    target_del = p_delete_admin.add_mutually_exclusive_group(required=True)
    target_del.add_argument("--id", type=int, dest="user_id")
    target_del.add_argument("--username", dest="username")

    p_set_archive = subparsers.add_parser("set-archive", help="Point a user's archive_db_ref at an already-existing, already-migrated database.")
    target2 = p_set_archive.add_mutually_exclusive_group(required=True)
    target2.add_argument("--id", type=int, dest="user_id")
    target2.add_argument("--username", dest="username")
    p_set_archive.add_argument("--db-name", required=True, dest="db_name")

    p_create_invite = subparsers.add_parser("create-invite", help="Generate a single-use invite token, attributed to the admin.")
    target3 = p_create_invite.add_mutually_exclusive_group(required=True)
    target3.add_argument("--id", type=int, dest="user_id")
    target3.add_argument("--created-by", dest="username", help="Username of the admin this invite is attributed to.")
    p_create_invite.add_argument("--expires-hours", type=int, default=24, dest="expires_hours")

    args = parser.parse_args()

    control_db.init_control_db(settings.control_database_url)
    try:
        if args.command == "create":
            cmd_create(args.username)
        elif args.command == "delete-admin":
            cmd_delete_admin(getattr(args, "user_id", None), getattr(args, "username", None))
        elif args.command == "set-archive":
            cmd_set_archive(getattr(args, "user_id", None), getattr(args, "username", None), args.db_name)
        elif args.command == "create-invite":
            cmd_create_invite(getattr(args, "user_id", None), getattr(args, "username", None), args.expires_hours)
    finally:
        control_db.close_db()


if __name__ == "__main__":
    main()