"""
Create or grant admin rights on the control database, outside the normal HTTP API.

Why this has to exist at all:
    control_db.schema.invites.created_by is NOT NULL with a foreign key to users.id,
    and there is no self-registration endpoint - every account (POST /auth/register) is created FROM an invite,
    and every invite is created BY an existing user.
    That's watertight for every user after the first one,
    but it means user #1 can never come into existence through the API at all: there is no admin yet to have created their invite.
    Something outside that loop has to insert the first row directly.
    This script is that something - and, since the need for direct DB access doesn't go away once user #1 exists
    (e.g. granting a second trusted person admin rights without handing them your own login),
    it supports four subcommands rather than being a disposable one-shot:

    create  - insert a brand-new user with is_admin=True from scratch (bypasses the invite flow entirely - this is the ONLY way to do that, by design;
              there is no equivalent HTTP endpoint and there shouldn't be one).
    promote - flip is_admin=True on a user who already registered normally through an invite
              (e.g. you want a second admin without sharing your own account).
    set-archive - point a user's archive_db_ref at an already-existing, already-migrated Postgres database name.
              A manual stand-in for real provisioning (POST /telegram/credentials triggering automatic database creation),
              which doesn't exist yet - see api/dependencies.py's get_archive_connection() for what actually reads this value.
    create-invite - generate a single-use invite token for POST /auth/register, since there's no admin UI for this yet either
              (see control_db/queries.py's create_invite() for the token generation itself - this subcommand is a thin wrapper:
              look up the admin by username/id, confirm, call it, print the token).
              Unlike the other three subcommands, this one needs an EXISTING admin to attribute the invite to (invites.created_by is NOT NULL) -
              so it can't help with the very first account either;
              `create` still has to run first, exactly once, before this subcommand has anyone to pass as --created-by.

Safety boundaries this script holds itself to (read before extending it):
    - Never touches telegram_api_id / telegram_api_hash / telegram_session_string / archive_db_ref for ANY user, in any subcommand except set-archive itself.
      create/promote call control_db.queries functions (create_admin_user / promote_user_to_admin) that only ever write to username/password_hash/is_admin,
      and create-invite's create_invite() only ever writes to the invites table - see those functions' own docstrings in control_db/queries.py.
      Promoting or creating an admin, or creating an invite, must never be a path to reading or altering someone's live Telegram credentials or archive location;
      this script has no code path that could do that even by accident, because the query layer it calls doesn't expose one.
    - Password is NEVER accepted as a command-line argument.
      A --password flag would land in shell history (~/.bash_history)
      and be visible to any other process on the same machine for as long as this one runs (`ps aux` shows full argv) - both are real, not hypothetical,
      exposures for a value that's about to become someone's login credential.
      getpass.getpass() is used instead: it reads from the terminal with input echo disabled and never touches argv, shell history,
      or (unless something in the environment is very unusually configured) any log file this process might write.
    - Password is never printed, logged, or included in any error message - only the fact that one was or wasn't provided/matched.
    - `promote` always looks up and displays the target user (id, username, current is_admin) BEFORE asking for confirmation,
      specifically so a typo'd --id doesn't silently grant admin to the wrong account.
      It also short-circuits with a plain message (no DB write, no audit row) if the target is already an admin,
      rather than writing a misleading "admin_promoted_via_script" audit entry for something that didn't actually change anything.
    - Every subcommand that writes anything requires an interactive y/N confirmation first.
      There is no --yes/--force flag - these are rare, high-privilege operations run by hand,
      not something meant to be scripted/automated, so there's no legitimate case for skipping the prompt.
    - Runs directly against control_database_url using this repo's own connection/query modules
      (control_db.connection, control_db.queries) - no ad-hoc SQL lives in this file.
      Any future change to what "safe to touch" means only has to be made once, in control_db/queries.py, not duplicated here.

Usage (run on the server, by whoever already has Postgres/VPS access - this is a local CLI tool, never reachable over HTTP):
    python scripts/manage_admin.py create --username alice
    python scripts/manage_admin.py promote --username alice
    python scripts/manage_admin.py promote --id 3
    python scripts/manage_admin.py set-archive --username alice --db-name alice_archive
    python scripts/manage_admin.py create-invite --created-by alice
    python scripts/manage_admin.py create-invite --created-by alice --expires-hours 48

`create` prompts for the new password twice (entry + confirmation),
same 8-character minimum as POST /auth/register (api/schemas/auth.py) -
there's no reason a script-created admin account should be allowed to be weaker than one created through the normal flow.

After `create`: the account has no Telegram credentials linked yet, and no archive_db_ref either - run `set-archive`
(against a database name you've already created and migrated by hand with `alembic upgrade head` pointed at it)
before that account can use /api/chats, /messages, /deleted, or /stats,
since get_archive_connection() (api/dependencies.py) 409s on a NULL archive_db_ref.

`create-invite` refuses to run if --created-by doesn't resolve to an admin - see the Decisions Log:
invite creation is meant to be an admin-only capability once it's a real HTTP endpoint,
and there's no reason this script's own version of that action should be looser than the feature it's standing in for.
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
from config import settings
from control_db import queries as cq
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
        if cq.get_user_by_username(conn, username) is not None:
            sys.exit(f"A user named '{username}' already exists. Use 'promote' instead if you meant to grant them admin.")

        password = _prompt_new_password()

        print(f"\nAbout to create a NEW ADMIN user '{username}'.")
        if not _confirm("Proceed?"):
            print("Cancelled. Nothing was written.")
            return

        password_hash = hash_password(password)
        try:
            user_id = cq.create_admin_user(conn, username, password_hash)
        except IntegrityError:
            sys.exit(f"A user named '{username}' already exists (created concurrently). Nothing was written.")

        print(f"Created admin user '{username}' (id={user_id}).")
        print("This account has no Telegram credentials linked yet - complete that via the normal")
        print("POST /telegram/link/send-code + /telegram/link/confirm + POST /telegram/credentials flow after logging in.")
    finally:
        conn.close()


def cmd_promote(user_id: int | None, username: str | None) -> None:
    conn = control_db.get_connection()
    try:
        user = cq.get_user_by_id(conn, user_id) if user_id is not None else cq.get_user_by_username(conn, username)
        if user is None:
            identifier = f"id={user_id}" if user_id is not None else f"username='{username}'"
            sys.exit(f"No user found with {identifier}. Nothing was written.")

        if user["is_admin"]:
            print(f"User '{user['username']}' (id={user['id']}) is already an admin. Nothing to do.")
            return

        print(f"\nAbout to grant admin rights to '{user['username']}' (id={user['id']}).")
        if not _confirm("Proceed?"):
            print("Cancelled. Nothing was written.")
            return

        cq.promote_user_to_admin(conn, user["id"])
        print(f"'{user['username']}' (id={user['id']}) is now an admin.")
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
            sys.exit(
                f"'{creator['username']}' (id={creator['id']}) isn't an admin. "
                "Invite creation is an admin-only action - use 'promote' first if this account should be one."
            )

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

    p_create = subparsers.add_parser("create", help="Create a brand-new admin user (bypasses the invite flow).")
    p_create.add_argument("--username", required=True)

    p_promote = subparsers.add_parser("promote", help="Grant admin rights to an existing user.")
    target = p_promote.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", type=int, dest="user_id")
    target.add_argument("--username", dest="username")

    p_set_archive = subparsers.add_parser("set-archive", help="Point a user's archive_db_ref at an already-existing, already-migrated database.")
    target2 = p_set_archive.add_mutually_exclusive_group(required=True)
    target2.add_argument("--id", type=int, dest="user_id")
    target2.add_argument("--username", dest="username")
    p_set_archive.add_argument("--db-name", required=True, dest="db_name")

    p_create_invite = subparsers.add_parser("create-invite", help="Generate a single-use invite token, attributed to an existing admin.")
    target3 = p_create_invite.add_mutually_exclusive_group(required=True)
    target3.add_argument("--id", type=int, dest="user_id")
    target3.add_argument("--created-by", dest="username", help="Username of the admin this invite is attributed to.")
    p_create_invite.add_argument("--expires-hours", type=int, default=24, dest="expires_hours")

    args = parser.parse_args()

    control_db.init_control_db(settings.control_database_url)
    try:
        if args.command == "create":
            cmd_create(args.username)
        elif args.command == "promote":
            cmd_promote(getattr(args, "user_id", None), getattr(args, "username", None))
        elif args.command == "set-archive":
            cmd_set_archive(getattr(args, "user_id", None), getattr(args, "username", None), args.db_name)
        elif args.command == "create-invite":
            cmd_create_invite(getattr(args, "user_id", None), getattr(args, "username", None), args.expires_hours)
    finally:
        control_db.close_db()


if __name__ == "__main__":
    main()