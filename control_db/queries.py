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
  - No is_admin enforcement of its own, anywhere in this module -
    every write function here trusts its caller exactly as much as create_invite() always has (see that function's own docstring).
    api/routes/admin.py is what actually restricts these to admins only (via api/dependencies.py's require_admin),
    same as scripts/manage_admin.py restricts itself to whoever already has shell access to run it - this module is the one place both of those callers meet,
    not the place that decides who's allowed to call it.
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


def list_users(conn: Connection) -> list[dict[str, Any]]:
    """
    Every user, oldest first (so the sole admin - always user #1, see create_admin_user()/the bootstrap-token path in api/routes/auth.py's register() -
    reliably sorts to the top of an admin-panel listing without the caller needing to know that in advance or sort by is_admin itself).

    Used only by GET /admin/users (api/routes/admin.py, require_admin-gated) - there's no equivalent CLI listing command in scripts/manage_admin.py;
    that script expects the operator to already know who they mean.
    Returns every column INCLUDING password_hash and the telegram_* credential columns - unlike get_user_by_id()'s other callers,
    which mostly feed a route's own response model that filters columns itself, so filtering here would just be redone at the route layer anyway.
    The route layer (not this function) is responsible for never putting password_hash or a live Telegram credential into an HTTP response.
    """
    rows = conn.execute(sql_text("SELECT * FROM users ORDER BY created_at ASC")).mappings().all()
    return [dict(row) for row in rows]


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


def list_invites(conn: Connection) -> list[dict[str, Any]]:
    """
    Every invite ever created, newest first, with the redeeming user's CURRENT username joined in
    (not `token` itself - see api/schemas/admin.py's AdminInviteListItemOut for why a past token can never be re-displayed).

    LEFT JOIN (not JOIN):
    used_by is nullable in two independent, unrelated ways - an invite that was never redeemed at all (used_by IS NULL from the start),
    and one whose redeemer has since been deleted (control_db.queries.delete_user_completely() nulls it via the ON DELETE SET NULL FK -
    see control_db/schema.py's own column comment).
    Both render as used_by_username=None to the caller;
    telling them apart (if ever needed) is what used_at IS NULL vs IS NOT NULL already distinguishes.
    created_by is deliberately NOT joined/returned here - every invite has exactly one creator (the sole admin, see control_db.schema.ix_users_single_admin),
    so surfacing it would only ever repeat the same name back for every row.

    Used only by GET /admin/invites (api/routes/admin.py, require_admin-gated).
    """
    rows = conn.execute(
        sql_text(
            """
            SELECT invites.id, invites.expires_at, invites.used_at, users.username AS used_by_username
            FROM invites
            LEFT JOIN users ON users.id = invites.used_by
            ORDER BY invites.id DESC
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


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


def delete_unused_invite(conn: Connection, invite_id: int, actor_id: int) -> bool:
    """
    Permanently delete an invite that nobody has redeemed yet (expired or still-valid alike).
    Returns True on success, False if no such invite exists.

    Raises ValueError (not caught here - the route layer decides how to report it, same split delete_user_completely() uses) if the invite HAS been redeemed:
    a used invite is the only record linking an account to the invite that created it (invites.used_by / used_at),
    so it is kept as registration history rather than being deletable.
    Deleting an unused one is different in kind - nothing was ever built on top of it, and for a still-valid token it doubles as "revoke":
    once the row is gone, register_user_via_invite() can no longer find it, so a token that leaked is dead immediately.

    "Unused" is enforced inside the DELETE itself (AND used_at IS NULL), not by a SELECT first:
    checking and then deleting would let someone redeem the invite in the gap between the two statements,
    and the redeemed row would then be deleted out from under the account that had just registered with it.
    The follow-up SELECT only runs when nothing was deleted, purely to tell "doesn't exist" apart from "already used".

    Not offered for expired invites as a separate case on purpose: an expired, unused invite is just a dead row, and this same function cleans it up.
    There is deliberately NO "revive an expired invite" counterpart -
    a token that was already handed out (and may have been forwarded or screenshotted) should not become valid again by flipping a date;
    creating a fresh invite is one click and yields a new, unrelated token.
    """
    try:
        result = conn.execute(
            sql_text("DELETE FROM invites WHERE id = :invite_id AND used_at IS NULL"),
            {"invite_id": invite_id},
        )
        if result.rowcount == 0:
            exists = conn.execute(
                sql_text("SELECT 1 FROM invites WHERE id = :invite_id"),
                {"invite_id": invite_id},
            ).first()
            conn.rollback()
            if exists is not None:
                raise ValueError(f"Invite {invite_id} has already been used and is kept as registration history.")
            return False
        _insert_audit_log(conn, event_type="invite_deleted_by_admin", actor_id=actor_id)
        conn.commit()
        return True
    except Exception:
        # Also catches the ValueError raised above; a second rollback on an already-rolled-back connection is harmless.
        conn.rollback()
        raise


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
    terms_version: str,
) -> int:
    """
    Create a new user, consume the invite that authorized it, and record the audit entry - all atomically.

    `terms_version` is the version of the Terms/Privacy Policy the person accepted; it is stored with the acceptance timestamp on the users row
    (the route layer has already refused the request if the checkbox wasn't ticked - this function only records it).

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
                INSERT INTO users (username, password_hash, terms_accepted_at, terms_version)
                VALUES (:username, :password_hash, now(), :terms_version)
                RETURNING id
                """
            ),
            {"username": username, "password_hash": password_hash, "terms_version": terms_version},
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
# admin bootstrap (no HTTP request/actor involved, OR the one-time bootstrap-token registration in api/routes/auth.py's register() -
# see admin_exists() below for what stops either path from ever creating a second admin)
# ---------------------------------------------------------------------------

def admin_exists(conn: Connection) -> bool:
    """
    Whether TeleVault's one admin account already exists.

    TeleVault has exactly one admin, full stop
    (Decisions Log - the promotion path that used to let a second account become an admin, promote_user_to_admin(), was removed;
    see scripts/manage_admin.py's own module docstring).
    control_db.schema.ix_users_single_admin is the actual enforcement
    (a partial unique index - Postgres itself refuses a second is_admin=true row even if every application-level check below somehow got skipped);
    this function is the cheap, non-throwing way for a caller to check BEFORE attempting a write that would otherwise fail with an IntegrityError -
    used by create_admin_user() below and by the bootstrap-token branch of register() to give a clear, specific error instead of a raw database exception.
    """
    return conn.execute(sql_text("SELECT 1 FROM users WHERE is_admin = true LIMIT 1")).first() is not None


def create_admin_user(
    conn: Connection,
    username: str,
    password_hash: str,
    event_type: str = "admin_created_via_script",
    terms_version: str | None = None,
) -> int:
    """
    Insert a brand-new user with is_admin=True, bypassing the invite flow entirely.

    Two callers, both outside the normal invite loop for the same underlying reason
    (see scripts/manage_admin.py's module docstring for the fuller "why does this bypass have to exist at all" reasoning):
      - scripts/manage_admin.py's `create` subcommand, run directly on the server by whoever has Postgres access - never reachable over HTTP.
        Default event_type ("admin_created_via_script") covers this caller.
      - api/routes/auth.py's register(), when the submitted invite_token matches settings.bootstrap_admin_token AND no admin exists yet (see admin_exists() above) -
        reachable over HTTP, but only ever succeeds ONCE per instance for that same reason.
        Passes event_type="admin_created_via_bootstrap_token" so auth_audit_log can tell the two paths apart later.
    Neither caller checks admin_exists() FOR you - both check it themselves first (see their own call sites) and this function does not re-check it,
    so control_db.schema.ix_users_single_admin is the only thing standing between a caller that forgets to check and an IntegrityError;
    that's intentional defense in depth, not a gap to close here,
    since a query-layer function silently swallowing "an admin already exists" would hide exactly the bug a caller needs to see.

    `terms_version`: the bootstrap-token path (a real person ticking the registration checkbox) passes the current version,
    which is recorded together with an acceptance timestamp;
    the shell script path leaves it None, and both columns stay NULL - nobody ticked anything there, so no acceptance is claimed.

    actor_id is left NULL in the resulting audit row on purpose: neither caller has an authenticated OTHER party to attribute this to - a script run at the shell,
    or the brand-new account acting on its own behalf - so recording one would fabricate an accountability trail that doesn't reflect what actually happened
    (see auth_audit_log's own docstring for why user_id and actor_id are allowed to differ / be absent).
    """
    try:
        new_user_id = conn.execute(
            sql_text(
                """
                INSERT INTO users (username, password_hash, is_admin, terms_accepted_at, terms_version)
                VALUES (
                    :username, :password_hash, true,
                    CASE WHEN CAST(:terms_version AS text) IS NULL THEN NULL ELSE now() END,
                    CAST(:terms_version AS text)
                )
                RETURNING id
                """
            ),
            {"username": username, "password_hash": password_hash, "terms_version": terms_version},
        ).scalar_one()
        _insert_audit_log(conn, event_type=event_type, user_id=new_user_id)
        conn.commit()
        return new_user_id
    except Exception:
        conn.rollback()
        raise


def lock_user(conn: Connection, user_id: int, actor_id: int) -> bool:
    """
    Set is_locked=True and immediately revoke every one of this user's outstanding refresh tokens
    (reusing revoke_all_refresh_tokens_and_log() above with the SAME revoked_reason/event_type api/routes/auth.py's own refresh() already uses for a locked account hitting /auth/refresh on its own - one vocabulary for "this account's sessions were killed because it's locked",
    regardless of whether locking or a blocked refresh attempt triggered it).
    Returns False if no such user exists (caller decides how to report that), True on success - including when the account was already locked,
    since re-locking an already-locked account isn't an error, just a no-op on is_locked itself
    (the refresh-token revocation still runs, harmlessly, against whatever's left un-revoked).

    Reversible ONLY by an admin calling unlock_user() below - there is deliberately no automatic expiry here,
    unlike the separate, self-clearing login-failure throttle in api/routes/auth.py's login()
    (see that function's own module-level comment on why the two are unrelated safeguards, not two versions of one thing).

    Deliberately does NOT touch password_hash, the telegram_* credential columns,
    or archive_db_ref - locking an account suspends its ability to authenticate, nothing else;
    the account's data is untouched and available again the moment an admin unlocks it.
    """
    result = conn.execute(
        sql_text("SELECT 1 FROM users WHERE id = :user_id"),
        {"user_id": user_id},
    ).first()
    if result is None:
        return False
    try:
        conn.execute(
            sql_text("UPDATE users SET is_locked = true WHERE id = :user_id"),
            {"user_id": user_id},
        )
        _insert_audit_log(conn, event_type="account_locked_by_admin", user_id=user_id, actor_id=actor_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # A separate call/transaction (not folded into the block above) since revoke_all_refresh_tokens_and_log() commits its own transaction -
    # see that function's own docstring;
    # nothing here needs the two atomic with each other;
    # a lock that "succeeded" but left one stale refresh token alive for a few extra moments is not a meaningfully different outcome
    # than one where both happened in lockstep.
    revoke_all_refresh_tokens_and_log(conn, user_id, "account_locked", "refresh_blocked_locked", ip_address=None, user_agent=None)
    return True


def unlock_user(conn: Connection, user_id: int, actor_id: int) -> bool:
    """
    Set is_locked=False. 
    Returns False if no such user exists, True on success (including a no-op unlock of an already-unlocked account, same "not an error" reasoning as lock_user() above).

    Does NOT re-issue or restore any of the refresh tokens lock_user() revoked - unlocking lets the account log in again from scratch (POST /auth/login),
    it does not resurrect whatever browser sessions were active at lock time;
    those are gone for good, same as after an ordinary password-change-driven revocation elsewhere in this module.
    """
    try:
        result = conn.execute(
            sql_text("UPDATE users SET is_locked = false WHERE id = :user_id"),
            {"user_id": user_id},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="account_unlocked_by_admin", user_id=user_id, actor_id=actor_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def delete_user_completely(conn: Connection, user_id: int, actor_id: int) -> str | None:
    """
    Irreversibly and completely remove a user:
    the users row itself, every refresh token, and every invite this user redeemed to register
    (its used_by pointer is nulled instead by the FK - see control_db/schema.py's own column comment -
    but the invite row survives for the admin's own invite-history bookkeeping).
    Everything that would block reusing the same username afterward is gone.
    Audit-log rows ABOUT this account (auth_audit_log.user_id = this user:
    its registration, sign-ins, failed sign-ins, Telegram link/unlink events - the rows that carry its IP address and browser string) are DELETED outright,
    not merely de-linked: a de-linked row would still hold a personal identifier (the IP) with nothing left to justify keeping it,
    and the Privacy Policy promises it goes away with the account.
    What stays: a single minimal "user_deleted" row
    (no IP, no user agent; user_id nulled by the FK, actor_id = whoever performed the deletion, which for an admin deleting someone is the admin themselves)
    so there is still an accountability trail for the deletion itself, and rows where this user merely appears as `actor_id` on SOMEONE ELSE's event
    (only ever possible for the admin account) - those belong to the other account's history.

    Called from TWO places, both requiring their own confirmation UX already, one step removed from this function
    (this function itself asks nothing and confirms nothing, matching every other write in this module - see this file's own module docstring):
      - DELETE /admin/users/{id} (api/routes/admin.py, require_admin-gated) - an admin deleting ANY non-admin user's account, actor_id = the admin's own id.
      - The planned self-service "delete my account" endpoint (not yet built) - actor_id = user_id itself,
        i.e. a user is always recorded as their own actor when deleting themselves, the same way clear_telegram_session() above does for an unlink.
    Also reachable for the admin account itself, but ONLY from scripts/manage_admin.py's delete-admin subcommand - deliberately never wired to any HTTP endpoint
    (see that script's own module docstring for why: an admin must never be able to delete themselves, or another admin, through the UI or API -
    this function doesn't special-case is_admin at all, the restriction lives entirely in which callers are allowed to reach it).

    Returns the deleted user's archive_db_ref (or None if they never had one)
    so the caller can DROP that database afterward via db.deprovisioning.drop_archive_database() - deliberately NOT done here:
    dropping a Postgres database can't run inside a transaction block at all
    (same constraint db/provisioning.py's own _create_database_if_missing() documents for CREATE DATABASE),
    so it can never be part of the same atomic unit as the control_db deletes below, and this function has no reason to depend on the db package at all otherwise.

    Raises ValueError (not caught here - the route/script layer decides how to report it) if this user created any invites
    (invites.created_by = user_id) - only ever possible for the admin account, since ordinary users never create invites;
    this is the one case where a hard failure is correct rather than silently cascading,
    since those invite rows would otherwise be left with a created_by pointing at nothing
    (created_by has no ON DELETE behavior - unlike used_by above - specifically so this can't happen silently).
    The caller (today, only scripts/manage_admin.py's delete-admin subcommand) is expected to ask the operator to deal with those invites by hand first.
    """
    user = get_user_by_id(conn, user_id)
    if user is None:
        return None

    created_invite_count = conn.execute(
        sql_text("SELECT count(*) FROM invites WHERE created_by = :user_id"),
        {"user_id": user_id},
    ).scalar_one()
    if created_invite_count > 0:
        raise ValueError(
            f"User {user_id} ('{user['username']}') created {created_invite_count} invite(s) and cannot be "
            "deleted while they still exist. Remove or reassign those invite rows first."
        )

    archive_db_ref = user["archive_db_ref"]
    try:
        # refresh_tokens (ON DELETE CASCADE) and invites.used_by (ON DELETE SET NULL) are handled by the database itself the moment the users row goes -
        # see control_db/schema.py's own column comments - so there is nothing to delete/null out for them explicitly here.
        # The audit entry for the deletion itself is inserted BEFORE the DELETE below (while user_id is still a valid reference) rather than after;
        # ON DELETE SET NULL then nulls its user_id the instant the DELETE below runs - see auth_audit_log's own column comments for why that's the intended outcome.
        # Purge BEFORE inserting the "user_deleted" row below, so that row (which also references this user_id until the FK nulls it) survives the purge.
        conn.execute(sql_text("DELETE FROM auth_audit_log WHERE user_id = :user_id"), {"user_id": user_id})
        _insert_audit_log(conn, event_type="user_deleted", user_id=user_id, actor_id=actor_id)
        result = conn.execute(sql_text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
        if result.rowcount == 0:
            # Deleted concurrently between the get_user_by_id() lookup above and here - vanishingly unlikely for a personal-scale app,
            # but rolling back rather than committing a bare audit row with nothing for it to have actually accompanied
            # keeps this function's own "all or nothing" promise.
            conn.rollback()
            return None
        conn.commit()
        return archive_db_ref
    except Exception:
        conn.rollback()
        raise


def set_archive_db_ref(conn: Connection, user_id: int, archive_db_ref: str) -> bool:
    """
    Set a user's archive_db_ref directly.
    Returns False if no such user exists, True on success.

    Called from scripts/manage_admin.py's `set-archive` subcommand
    (the manual path - still useful as a fallback when automatic provisioning can't run, e.g. db/provisioning.py's ArchiveProvisioningError)
    and from api/routes/archive.py's POST /archive/provision (the automatic path, once db/provisioning.py has actually created and migrated the database).
    Either way, this function only RECORDS the reference;
    it does not create, verify, or migrate the database that reference names -
    the caller is responsible for making sure `archive_db_ref` actually names a real,
    already-migrated Postgres database on the same server as database_url before pointing a user at it,
    since get_archive_connection() will happily try to connect to whatever is written here.

    Deliberately touches ONLY archive_db_ref - never password_hash, never is_admin, never the telegram_* credential columns,
    for the same reason promote_user_to_admin() above stays narrow: one function should do exactly the one thing its name says.
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


def clear_telegram_session(conn: Connection, user_id: int) -> bool:
    """
    Unlink Telegram: clear telegram_session_string back to NULL.
    Returns False if no such user exists, True on success.

    Deliberately touches ONLY telegram_session_string - NOT telegram_api_id/telegram_api_hash,
    unlike set_telegram_credentials() above (which clears the session as a SIDE EFFECT of a credentials change).
    Here it's the other way around:
    the person is deliberately disconnecting Telegram but has no reason to re-enter their api_id/api_hash to do it again afterwards - those came from my.telegram.org,
    not from Telegram's own per-login session, and stay valid regardless of which Telegram account is or isn't currently linked through them.
    Also does NOT touch archive_db_ref: unlinking Telegram is not deleting an account's archived history, and there is no reason the two should be coupled.

    Self-service, same posture as set_telegram_credentials()/set_telegram_session() above - called with the caller's OWN user_id from api/routes/telegram.py
    (Depends(get_current_user)), not an admin-only action.
    Safe to call on an account with no session at all (rowcount still reflects the user row existing, not whether telegram_session_string actually changed) -
    DELETE /telegram/session is idempotent by design, since "make sure Telegram is unlinked" shouldn't fail just because it already was.
    """
    try:
        result = conn.execute(
            sql_text("UPDATE users SET telegram_session_string = NULL WHERE id = :user_id"),
            {"user_id": user_id},
        )
        if result.rowcount == 0:
            conn.rollback()
            return False
        _insert_audit_log(conn, event_type="telegram_unlinked", user_id=user_id, actor_id=user_id)
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