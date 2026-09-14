"""
Defines the "control DB" schema as SQLAlchemy Core Table objects -
the single shared database that holds account/auth data for TeleVault's multi-user feature, as opposed to the per-user archive schema (db/schema.py).

Why a separate database rather than more tables in db/schema.py's existing database:
    Each user gets their own isolated archive database (their chats/messages/etc. never share a database with anyone else's).
    Account data - who exists, their password hash, their refresh tokens, the audit trail - is the opposite:
    exactly one copy, shared across every user, and it must exist and be queryable *before* a user's personal archive database can even be identified
    (users.archive_db_ref below is how a control-DB row points at "which archive database is this user's").
    Mixing the two into one database/metadata would mean every future per-user archive database also carries
    a full copy of every other user's account row - not just redundant,
    but a real information-boundary violation for something explicitly scoped to trusted multi-user rather than public multi-tenant.

Table overview:
  - users            : one row per person with a TeleVault account (you are the first row, is_admin=true)
  - invites          : single-use invite tokens; the only way a new users row gets created (no self-registration)
  - refresh_tokens   : one row per issued refresh token, for revocation - see its own docstring for the rotation model
  - auth_audit_log   : append-only log of security-relevant events (logins, lockouts, invite use, etc.)

This module is consumed by:
  - alembic_control/, a SEPARATE Alembic setup from alembic/ (which manages db/schema.py's metadata instead) -
    see alembic_control/env.py's module docstring for why two independent Alembic setups exist in this repo.
  - Whatever auth/account code reads or writes these tables later (not yet written - this is schema only).

Encryption note (telegram_api_id / telegram_api_hash / telegram_session_string on `users`):
    These are marked "(encrypted)" in the original planning notes because they are Telegram *credentials*,
    not just Telegram *data* - unlike everything in db/schema.py (which is the content the user chose to archive),
    a leaked session string is a live, usable login to that person's real Telegram account.
    They're declared as plain Text here because SQLAlchemy Core has no column-level encryption of its own;
    the actual encrypt/decrypt (planned: Fernet, symmetric, one application-held key)
    happens in the application layer before a value is written to / after it's read from this Text column - not yet implemented,
    flagged in the handoff notes this migration was built from.
    This module only shapes the column as "opaque encrypted text goes here", not how it gets that way.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    Table,
    Text,
    TIMESTAMP,
    func,
    text,
)

# Separate registry from db/schema.py's `metadata` - see this module's docstring for why these live in a different database entirely,
# not just a different Table object in the same one.
metadata = MetaData()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
# One row per person with a TeleVault account.
# Rows are only ever created via a valid, unused invite token (see `invites` below) - there is no public self-registration endpoint.
#
# archive_db_ref: identifies which per-user archive database (db/schema.py's schema, provisioned separately) this user's messages live in.
# Nullable here because the exact provisioning step/timing (e.g. "at invite creation" vs "at first successful Telegram link")
# wasn't pinned down in the planning this migration is based on - left open rather than guessed at,
# since forcing NOT NULL now would just move that undecided question into "what dummy value do we insert" instead of resolving it.
#
# password_changed_at defaults to created_at's value (func.now(), same instant) rather than being left NULL for a brand-new user,
# since "revoke every OTHER session on password change" (the confirmed scope decision)
# reads more simply as "not equal to created_at" than as a NULL special-case for accounts that have never changed their password.
users = Table(
    "users",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("username", Text, nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("password_changed_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("last_login_at", TIMESTAMP(timezone=True)),
    Column("is_admin", Boolean, nullable=False, server_default=text("false")),
    Column("is_locked", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("telegram_api_id", Text),
    Column("telegram_api_hash", Text),
    Column("telegram_session_string", Text),
    Column("archive_db_ref", Text),
)


# ---------------------------------------------------------------------------
# invites
# ---------------------------------------------------------------------------
# Single-use invite tokens.
# created_by is required (only an admin creates these, via POST /admin/invites);
# used_by/used_at stay NULL until POST /auth/register successfully consumes the token,
# and are set together - "used" is defined as used_at IS NOT NULL, not as a separate boolean, so the two can never disagree.
invites = Table(
    "invites",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("token", Text, nullable=False, unique=True),
    Column("created_by", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("used_at", TIMESTAMP(timezone=True)),
    Column("used_by", BigInteger, ForeignKey("users.id")),
)


# ---------------------------------------------------------------------------
# refresh_tokens
# ---------------------------------------------------------------------------
# One row per issued refresh token.
# The JWT itself is never stored - only `jti` (the token's unique ID claim),
# so a stolen database dump doesn't hand out usable tokens, only the ability to revoke ones that already exist.
# Revocation-by-rotation: each successful /auth/refresh call sets revoked_at on the row being used and inserts a new one -
# a jti with revoked_at set that a client tries to redeem anyway is a strong signal of a stolen/replayed refresh token,
# since the legitimate holder would have moved on to its replacement.
refresh_tokens = Table(
    "refresh_tokens",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("jti", Text, nullable=False, unique=True),
    Column("issued_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("revoked_at", TIMESTAMP(timezone=True)),
    # Covers "revoke every refresh token for this user" (password change / admin lock) and "list this user's active sessions" -
    # both filter on user_id first.
    Index("idx_refresh_tokens_user_id", "user_id"),
)


# ---------------------------------------------------------------------------
# auth_audit_log
# ---------------------------------------------------------------------------
# Append-only.
# Security-relevant events only (logins, lockouts, password changes, invite issuance/use, Telegram linking) -
# deliberately NOT a general-purpose UI telemetry table or a per-message access log (out of scope, per the planning this is based on).
#
# user_id is nullable: some events have no subject yet (e.g. a failed login against a username that doesn't exist at all).
# actor_id is separate from user_id because they can differ - e.g. an admin locking a *different* user's account:
# actor_id is the admin, user_id is whose account was acted on.
#
# event_type is plain Text rather than CHECK-constrained to a fixed list
# (contrast `chats.chat_type` and `message_deletions.deleted_by_inference` in db/schema.py, which ARE constrained):
# the exact vocabulary of events wasn't finalized in the planning this migration is based on,
# and inventing one now risks locking in values the actual endpoint code won't end up using.
# Worth adding a CHECK constraint in a follow-up migration once the real call sites (and their event names) exist.
auth_audit_log = Table(
    "auth_audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id")),
    Column("event_type", Text, nullable=False),
    Column("actor_id", BigInteger, ForeignKey("users.id")),
    Column("ip_address", Text),
    Column("user_agent", Text),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    # Covers "show this user's audit history" (user_id) and "show recent events across everyone" (created_at) -
    # the two access patterns an admin-only audit view actually needs.
    Index("idx_auth_audit_log_user_id", "user_id"),
    Index("idx_auth_audit_log_created_at", "created_at"),
)