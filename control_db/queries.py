"""
All query operations against the control database (users/invites/refresh_tokens/auth_audit_log).

Follows the same conventions as db/queries.py (see that module's docstring for the fuller reasoning behind each choice)
so the two query layers read consistently:
  - Every function takes a Connection as its first argument.
    No global state.
  - Explicit try/commit/except-rollback around every write, since Postgres aborts the whole transaction on the first error,
    not just the failing statement.
  - Reads return plain dicts (via .mappings()) or None, no ORM, no Pydantic in this module - validation happens in the route layer.
  - Multi-statement operations that must succeed or fail together (e.g. "insert the user AND mark the invite used") are wrapped in one transaction here,
    not split across two calls the route layer would have to remember to pair up itself.

What this module deliberately does NOT do:
  - No password hashing/verification, no JWT encode/decode, no Fernet encrypt/decrypt.
    Those stay in utils/security.py and utils/crypto.py;
    this module only ever sees already-hashed or already-encrypted values.
    Keeps "how do we prove a password is right" and "how do we store a row" as two separate concerns.
  - No admin/invite-creation HTTP endpoints yet (out of scope - see project brief) - create_invite() below exists,
    but its only caller is scripts/manage_admin.py, not a route.
    An HTTP endpoint calling it later still needs its own is_admin check first;
    this module doesn't enforce that itself (see this docstring's second point above).
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# users - reads
# ---------------------------------------------------------------------------

def get_user_by_username(conn: Connection, username: str) -> dict[str, Any] | None:
    """Look up a user by username (case-sensitive - usernames are stored as given, see users.username's unique constraint)."""
    row = conn.execute(
        sql_text("SELECT * FROM users WHERE username = :username"),
        {"username": username},
    ).mappings().first()
    return dict(row) if row else None


def get_user_by_id(conn: Connection, user_id: int) -> dict[str, Any] | None:
    """Look up a user by primary key."""
    row = conn.execute(
        sql_text("SELECT * FROM users WHERE id = :user_id"),
        {"user_id": user_id},
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# invites - reads
# ---------------------------------------------------------------------------

def get_valid_invite_by_token(conn: Connection, token: str) -> dict[str, Any] | None:
    """
    Look up an invite by its token, but ONLY if it's still usable: not already consumed (used_at IS NULL) and not past expires_at.

    Returning None for an expired-or-used invite (rather than returning the row and making the route layer inspect used_at/expires_at itself)
    keeps "is this invite still good" defined in exactly one place.
    """
    row = conn.execute(
        sql_text(
            """
            SELECT * FROM invites
            WHERE token = :token
              AND used_at IS NULL
              AND expires_at > now()
            """
        ),
        {"token": token},
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# invites - writes
# ---------------------------------------------------------------------------

def create_invite(conn: Connection, created_by: int, expires_at: datetime) -> str:
    """
    Insert a new, single-use invite token and return it.

    The token itself is generated HERE, not by the caller, so there's exactly one place in the codebase that decides what an invite token looks like
    (secrets.token_urlsafe - cryptographically random, URL-safe, no separators to trip up copy/paste).
    Currently only called from scripts/manage_admin.py's create-invite subcommand;
    a future HTTP endpoint for admins to create invites from the UI would call this same function rather than duplicating the generation logic,
    but would need its own is_admin check before calling it - this function trusts created_by exactly as much as every other write in this module trusts its caller
    (see this file's own module docstring).

    Does NOT verify created_by is actually an admin, or even that the id exists - a bad id fails loudly via the NOT NULL foreign key constraint on invites.created_by
    (IntegrityError, uncaught here, same as register_user_via_invite()'s duplicate-username case above) rather than silently.

    actor_id is set to created_by, not left NULL: unlike create_admin_user()'s bootstrap case (no admin exists yet, so there's genuinely no actor to record),
    an invite always has a real, already-authenticated-or-at-least-script-run admin behind it.
    """
    token = secrets.token_urlsafe(24)
    try:
        conn.execute(
            sql_text(
                """
                INSERT INTO invites (token, created_by, expires_at)
                VALUES (:token, :created_by, :expires_at)
                """
            ),
            {"token": token, "created_by": created_by, "expires_at": expires_at},
        )
        _insert_audit_log(conn, event_type="invite_created_via_script", actor_id=created_by)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return token


# ---------------------------------------------------------------------------
# registration (users + invites, one transaction)
# ---------------------------------------------------------------------------

def register_user_via_invite(
    conn: Connection,
    invite_id: int,
    username: str,
    password_hash: str,
    ip_address: str | None,
    user_agent: str | None,
) -> int:
    """
    Create a new user, consume the invite that authorized it, and record the audit entry - all atomically.

    All three happen in one transaction so a crash partway through can never leave a "used" invite with no corresponding user,
    a new user whose invite still looks unused (and so reusable by someone else), or a user with no audit trail of how their account came to exist -
    see invites' own docstring in control_db/schema.py for why used_at/used_by are always set together.

    Raises sqlalchemy.exc.IntegrityError (uncaught) on a duplicate username - the route layer turns that into a 409, not this function;
    a query layer guessing at HTTP status codes would be the wrong place for that decision.
    """
    try:
        new_user_id = conn.execute(
            sql_text(
                """
                INSERT INTO users (username, password_hash)
                VALUES (:username, :password_hash)
                RETURNING id
                """
            ),
            {"username": username, "password_hash": password_hash},
        ).scalar_one()

        conn.execute(
            sql_text(
                """
                UPDATE invites
                SET used_at = now(), used_by = :user_id
                WHERE id = :invite_id
                """
            ),
            {"user_id": new_user_id, "invite_id": invite_id},
        )
        # actor_id = the new user themselves - registration is a self-action, not something done
        # to them by someone else (contrast an admin locking a DIFFERENT user's account, where the two differ).
        _insert_audit_log(
            conn,
            event_type="user_registered",
            user_id=new_user_id,
            actor_id=new_user_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        conn.commit()
        return new_user_id
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

def record_login_success(conn: Connection, user_id: int, ip_address: str | None, user_agent: str | None) -> None:
    """Update last_login_at and append the audit-log entry for a successful login, atomically."""
    try:
        conn.execute(
            sql_text("UPDATE users SET last_login_at = now() WHERE id = :user_id"),
            {"user_id": user_id},
        )
        _insert_audit_log(conn, event_type="login_succeeded", user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def count_recent_login_failures_for_user(conn: Connection, user_id: int, window: timedelta) -> int:
    """
    Count login_failed audit events for a SPECIFIC, existing account within the last `window`.

    Keyed on user_id, not IP - this protects one account from repeated guessing without ever affecting any other account's ability to log in,
    even from the exact same network address (an earlier IP-keyed version of this check didn't have that property:
    one person's typos could exhaust the shared bucket for everyone on the same network/VPN).
    """
    row = conn.execute(
        sql_text(
            """
            SELECT COUNT(*) FROM auth_audit_log
            WHERE event_type = 'login_failed'
              AND user_id = :user_id
              AND created_at > :since
            """
        ),
        {"user_id": user_id, "since": datetime.now(timezone.utc) - window},
    ).scalar_one()
    return int(row)


def count_recent_login_failures_for_unknown_username(conn: Connection, ip_address: str | None, window: timedelta) -> int:
    """
    Count login_failed audit events against USERNAMES THAT DON'T EXIST, from this IP, within `window`.

    IP-keyed on purpose, unlike count_recent_login_failures_for_user() above - and safe to be,
    for a reason that function's per-account version isn't:
    there is no real account behind these attempts, so throttling by IP here can never lock a legitimate person out of their own account.
    At worst it delays someone repeatedly mistyping their OWN username (which doesn't exist yet as typed)
    sharing a network with someone probing for valid usernames - an acceptable trade against stopping username-enumeration attempts,
    which have nothing else to key on (see this table's own docstring in control_db/schema.py: user_id is nullable for exactly this case,
    and there's no column anywhere recording the raw attempted username).

    CAVEAT (read before relying on this in production): if this API sits behind Nginx as documented in api/server.py,
    ip_address here is whatever the request handler was given - if Nginx isn't configured to forward the real client IP
    (`proxy_set_header X-Forwarded-For $remote_addr;` or equivalent),
    every request arrives from Nginx's own loopback address, collapsing this counter across every real client.
    Since this path only ever throttles guesses against nonexistent usernames (never a real account),
    the worst case of that misconfiguration is a shared delay on username-guessing attempts, not a lockout of anyone's actual account.
    """
    if ip_address is None:
        return 0
    row = conn.execute(
        sql_text(
            """
            SELECT COUNT(*) FROM auth_audit_log
            WHERE event_type = 'login_failed'
              AND user_id IS NULL
              AND ip_address = :ip_address
              AND created_at > :since
            """
        ),
        {"ip_address": ip_address, "since": datetime.now(timezone.utc) - window},
    ).scalar_one()
    return int(row)


def record_login_failure(conn: Connection, user_id: int | None, ip_address: str | None, user_agent: str | None) -> None:
    """Append a login_failed audit event. user_id is None when the attempted username doesn't exist at all."""
    try:
        _insert_audit_log(conn, event_type="login_failed", user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_login_blocked(conn: Connection, event_type: str, user_id: int | None, ip_address: str | None, user_agent: str | None) -> None:
    """
    Append an audit event for a login attempt rejected before password verification
    (event_type is 'login_blocked_locked', 'login_blocked_rate_limited_account', or 'login_blocked_rate_limited_unknown_username').

    Deliberately a different event_type from login_failed - these attempts never got as far as checking a password,
    and counting them alongside login_failed in count_recent_login_failures_for_user()/count_recent_login_failures_for_unknown_username()
    would let one rate-limit rejection cascade into a longer block by re-triggering itself.
    """
    try:
        _insert_audit_log(conn, event_type=event_type, user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_refresh_token_and_owner(conn: Connection, jti: str) -> dict[str, Any] | None:
    """
    Look up a refresh_tokens row by jti, joined with the owning user's is_admin/is_locked - /auth/refresh needs both the token row
    (for revoked_at/expires_at) and the user row (for is_locked) in the same round trip, and there's no reason to make it two queries.
    """
    row = conn.execute(
        sql_text(
            """
            SELECT rt.*, u.is_locked AS owner_is_locked, u.is_admin AS owner_is_admin
            FROM refresh_tokens rt
            JOIN users u ON u.id = rt.user_id
            WHERE rt.jti = :jti
            """
        ),
        {"jti": jti},
    ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# refresh tokens
# ---------------------------------------------------------------------------

def insert_refresh_token(conn: Connection, user_id: int, jti: str, expires_at: datetime) -> None:
    """Persist a newly-issued refresh token's revocation record. Commits on success, the caller decides what else shares this transaction."""
    try:
        conn.execute(
            sql_text(
                """
                INSERT INTO refresh_tokens (user_id, jti, expires_at)
                VALUES (:user_id, :jti, :expires_at)
                """
            ),
            {"user_id": user_id, "jti": jti, "expires_at": expires_at},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rotate_refresh_token(
    conn: Connection,
    old_jti: str,
    user_id: int,
    new_jti: str,
    new_expires_at: datetime,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """
    Revoke `old_jti` (reason='rotated'), insert its replacement, and record the audit entry - atomically.
    The rotation model itself is documented on refresh_tokens in control_db/schema.py.
    """
    try:
        conn.execute(
            sql_text("UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'rotated' WHERE jti = :jti"),
            {"jti": old_jti},
        )
        conn.execute(
            sql_text(
                """
                INSERT INTO refresh_tokens (user_id, jti, expires_at)
                VALUES (:user_id, :jti, :expires_at)
                """
            ),
            {"user_id": user_id, "jti": new_jti, "expires_at": new_expires_at},
        )
        _insert_audit_log(conn, event_type="token_refreshed", user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def revoke_refresh_token_for_logout(conn: Connection, jti: str, user_id: int, ip_address: str | None, user_agent: str | None) -> None:
    """Revoke a single refresh token (reason='logout') and record the audit entry, atomically (used by /auth/logout)."""
    try:
        conn.execute(
            sql_text("UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'logout' WHERE jti = :jti AND revoked_at IS NULL"),
            {"jti": jti},
        )
        _insert_audit_log(conn, event_type="logout", user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def revoke_all_refresh_tokens_and_log(
    conn: Connection,
    user_id: int,
    revoked_reason: str,
    event_type: str,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """
    Revoke every still-active refresh token for a user and record why, atomically.

    Used in two situations that both mean "stop trusting this user's outstanding refresh tokens immediately":
    a locked account attempting /auth/refresh (revoked_reason='account_locked', event_type='refresh_blocked_locked'),
    and reuse of an already-revoked refresh token whose OWN revoked_reason indicates it wasn't just an ordinary logout - a strong signal of theft,
    since the legitimate holder would have moved on to its replacement
    (revoked_reason='reuse_detected', event_type='refresh_reuse_detected';
    see refresh_tokens' docstring in control_db/schema.py and api/routes/auth.py's refresh() for the reasoning behind that inference
    and why plain logout' is deliberately NOT treated this way).

    revoked_reason and event_type are passed separately (rather than derived from one another)
    because they serve different audiences at different granularity: revoked_reason is a short,
    fixed vocabulary read by CODE (refresh()'s own branching logic on re-presentation),
    while event_type is the free-form, descriptive string read by a HUMAN skimming auth_audit_log.
    """
    try:
        conn.execute(
            sql_text(
                """
                UPDATE refresh_tokens
                SET revoked_at = now(), revoked_reason = :revoked_reason
                WHERE user_id = :user_id AND revoked_at IS NULL
                """
            ),
            {"user_id": user_id, "revoked_reason": revoked_reason},
        )
        _insert_audit_log(conn, event_type=event_type, user_id=user_id, ip_address=ip_address, user_agent=user_agent)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# admin bootstrap (scripts/manage_admin.py - no HTTP request/actor involved)
# ---------------------------------------------------------------------------

def create_admin_user(conn: Connection, username: str, password_hash: str) -> int:
    """
    Insert a brand-new user with is_admin=True, bypassing the invite flow entirely.

    Only ever called from scripts/manage_admin.py, run directly on the server by whoever has Postgres access - never reachable over HTTP.
    See that script's module docstring for why this bypass has to exist (the invite flow's own FK constraint means it cannot create user #1).

    actor_id is left NULL in the resulting audit row on purpose: there is no authenticated actor for a script run at the shell,
    so recording one would fabricate an accountability trail that doesn't reflect what actually happened
    (see auth_audit_log's own docstring for why user_id and actor_id are allowed to differ / be absent).
    """
    try:
        new_user_id = conn.execute(
            sql_text(
                """
                INSERT INTO users (username, password_hash, is_admin)
                VALUES (:username, :password_hash, true)
                RETURNING id
                """
            ),
            {"username": username, "password_hash": password_hash},
        ).scalar_one()
        _insert_audit_log(conn, event_type="admin_created_via_script", user_id=new_user_id)
        conn.commit()
        return new_user_id
    except Exception:
        conn.rollback()
        raise


def promote_user_to_admin(conn: Connection, user_id: int) -> bool:
    """
    Flip is_admin to True for an existing user.
    Returns False if no such user exists (caller decides how to report that), True on success.

    Deliberately touches ONLY is_admin - never password_hash, never the telegram_* credential columns, never archive_db_ref.
    Promoting someone to admin must never require or allow touching their password or their linked Telegram credentials;
    those are the person's own, set by their own login/link flow, and a script run by someone else has no business reading or altering them.
    See scripts/manage_admin.py's module docstring for the fuller reasoning.
    """
    try:
        result = conn.execute(
            sql_text("UPDATE users SET is_admin = true WHERE id = :user_id"),
            {"user_id": user_id},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="admin_promoted_via_script", user_id=user_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def set_archive_db_ref(conn: Connection, user_id: int, archive_db_ref: str) -> bool:
    """
    Set a user's archive_db_ref directly.
    Returns False if no such user exists, True on success.

    Only ever called from scripts/manage_admin.py's `set-archive` subcommand - a manual stand-in for real provisioning,
    which doesn't exist yet (see that script's module docstring and api/dependencies.py's get_archive_connection()).
    This function only RECORDS the reference;
    it does not create, verify, or migrate the database that reference names -
    the caller is responsible for making sure `archive_db_ref` actually names a real,
    already-migrated Postgres database on the same server as control_database_url before pointing a user at it,
    since get_archive_connection() will happily try to connect to whatever is written here.

    Deliberately touches ONLY archive_db_ref - never password_hash, never is_admin, never the telegram_* credential columns,
    for the same reason promote_user_to_admin() above stays narrow: one script subcommand should do exactly the one thing its name says.
    """
    try:
        result = conn.execute(
            sql_text("UPDATE users SET archive_db_ref = :archive_db_ref WHERE id = :user_id"),
            {"user_id": user_id, "archive_db_ref": archive_db_ref},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="archive_db_ref_set_via_script", user_id=user_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# telegram credentials / session (self-service, via api/routes/telegram.py)
# ---------------------------------------------------------------------------

def set_telegram_credentials(conn: Connection, user_id: int, encrypted_api_id: str, encrypted_api_hash: str) -> bool:
    """
    Store a user's own Telegram app credentials (api_id/api_hash from my.telegram.org), already Fernet-encrypted by the caller
    (see utils/crypto.py's encrypt_secret() - this function never touches plaintext, it only persists whatever ciphertext it's handed).
    Returns False if no such user exists, True on success.

    Also clears telegram_session_string back to NULL:
    a previously-linked session was authenticated under the OLD api_id/api_hash pair (or is being re-entered for another reason),
    and Telegram sessions aren't guaranteed portable across a credentials change -
    safer to require the send-code/confirm handshake to run again than to keep trusting a session that might silently misbehave.
    Does NOT touch archive_db_ref - resubmitting credentials shouldn't undo which archive this account is already pointed at.

    Self-service, unlike set_archive_db_ref() above:
    called with the caller's OWN user_id from api/routes/telegram.py (Depends(get_current_user), not require_instance_owner) -
    every account can set its own Telegram credentials, this isn't an admin-only or instance-owner-only action.
    """
    try:
        result = conn.execute(
            sql_text(
                """
                UPDATE users
                SET telegram_api_id = :api_id, telegram_api_hash = :api_hash, telegram_session_string = NULL
                WHERE id = :user_id
                """
            ),
            {"user_id": user_id, "api_id": encrypted_api_id, "api_hash": encrypted_api_hash},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="telegram_credentials_set", user_id=user_id, actor_id=user_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def set_telegram_session(conn: Connection, user_id: int, encrypted_session_string: str) -> bool:
    """
    Store a user's Telegram session string once the send-code/confirm handshake succeeds - already
    Fernet-encrypted by the caller, same reasoning as set_telegram_credentials() above. Returns False
    if no such user exists, True on success.

    Deliberately touches ONLY telegram_session_string - never api_id/api_hash (those were already
    written by set_telegram_credentials() earlier in the same flow) and never archive_db_ref
    (provisioning the actual archive database is a separate, not-yet-built step - see
    api/routes/telegram.py's module docstring).
    """
    try:
        result = conn.execute(
            sql_text("UPDATE users SET telegram_session_string = :session_string WHERE id = :user_id"),
            {"user_id": user_id, "session_string": encrypted_session_string},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="telegram_linked", user_id=user_id, actor_id=user_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _insert_audit_log(
    conn: Connection,
    event_type: str,
    user_id: int | None = None,
    actor_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Append one row to auth_audit_log.
    Does NOT commit - the caller commits as part of its own transaction,
    since every audit entry so far is written alongside another state change it's documenting
    (a login, a rotation, a promotion) and the two must land together or not at all.
    """
    conn.execute(
        sql_text(
            """
            INSERT INTO auth_audit_log (user_id, event_type, actor_id, ip_address, user_agent)
            VALUES (:user_id, :event_type, :actor_id, :ip_address, :user_agent)
            """
        ),
        {"user_id": user_id, "event_type": event_type, "actor_id": actor_id, "ip_address": ip_address, "user_agent": user_agent},
    )