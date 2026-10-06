"""
Loads all runtime configuration from environment variables (via a .env file).
 
The settings are exposed as a single frozen dataclass instance - `settings` - imported directly wherever needed:
 
    from config import settings
    print(settings.db_path)
 
Why a dataclass rather than reading os.environ inline?
  - One import gives you everything; no hunting for os.getenv() calls scattered around the codebase.
  - The frozen=True flag prevents accidental mutation after startup.
  - Type annotations document what each setting is supposed to be.
  - Missing required values fail loudly at startup, not halfway through a run.
 
Required .env keys:       TG_API_ID, TG_API_HASH, TG_PHONE, FERNET_KEY, JWT_SECRET
Optional (have defaults): DB_PATH, DATABASE_URL, CONTROL_DATABASE_URL, SESSION_NAME, LOG_LEVEL, LOG_FILE
Optional (no default - None means "not configured"): TELEVAULT_BOOTSTRAP_ADMIN_TOKEN (see Settings.bootstrap_admin_token below)
"""

import os
import sys
import logging
from dataclasses import dataclass
from pathlib import Path

# python-dotenv reads the .env file and injects values into os.environ.
# It does nothing if the variables are already set (e.g. in a real shell environment on the VPS), so it's safe to leave this call in production.
try:
    from dotenv import load_dotenv
except ImportError:
    # Fail clearly rather than silently running without .env support.
    sys.exit(
        "ERROR: Python package 'python-dotenv' is required.\n"
        "Install it with 'pip install python-dotenv' and try again."
    )


logger = logging.getLogger(__name__)


# Resolve .env relative to this file, not the working directory.
# That way `python -m televault` works from any directory.
_ENV_PATH = Path(__file__).parent / ".env"

# Load environment variables from .env file.
load_dotenv(_ENV_PATH)


def _require(key: str) -> str:
    """
    Read a required environment variable.
    Exits immediately with a clear message if it's missing or empty.
    """
    value = os.getenv(key, "").strip()
    if not value:
        sys.exit(
            f"[config] Required environment variable '{key}' is not set. "
            f"Check your .env file."
        )
    return value

def _optional(key: str, default: str) -> str:
    """
    Read an optional environment variable, falling back to a default.
    """
    return os.getenv(key, default).strip() or default


@dataclass(frozen=True)
class Settings:
    """
    All runtime configuration for TeleVault.
 
    Frozen so nothing can accidentally overwrite a setting after startup.
    Attributes map 1-to-1 with .env keys (lowercased and without the TG_ prefix where they're Telegram-specific).
    """

    # --- Telegram credentials ---
    # Obtain these from https://my.telegram.org -> API development tools.
    # Treat them like passwords: never commit, never log.
    api_id: int
    api_hash: str
    phone: str

    # Name for the Telethon session file (stored as <name>.session).
    # Changing this forces a fresh login - keep it stable.
    session_name: str

    # --- Storage (SQLite - active storage layer for now) ---
    db_path: str

    # --- Storage (PostgreSQL - migration in progress, see CHANGELOG "1.2.0").
    # Not read by anything yet; db/connection.py still talks to SQLite only.
    # Defaults to matching docker-compose.yml's local dev Postgres service. ---
    database_url: str

    # --- Storage (control DB - auth/multi-user feature) ---
    # Deliberately a SEPARATE database from database_url above, not just a separate schema/table prefix within it:
    # database_url points at ONE user's archive (chats/messages/etc.);
    # this points at the single shared DB holding accounts, invites, refresh tokens, and audit log -
    # see control_db/schema.py's module docstring for the full reasoning.
    # Same Postgres instance as database_url by default (just a different database name) - nothing stops pointing
    # this at a different host/instance entirely later, since it's a fully independent connection string.
    control_database_url: str

    # --- Encryption (control DB credential columns: users.telegram_api_id/api_hash/session_string) ---
    # Symmetric (Fernet) key used by utils/crypto.py to encrypt/decrypt those three columns before they touch the database.
    # Required, not optional-with-a-default:
    # an auto-generated default here would mean every fresh checkout silently gets its OWN key,
    # unable to decrypt anything encrypted under a previous run's default - a missing key should fail loudly at startup
    # (see this file's module docstring), not silently generate a new one that can't read existing data.
    fernet_key: str

    # --- Auth (JWT access/refresh tokens - see utils/security.py) ---
    # Separate from fernet_key above on purpose: that key ENCRYPTS values for storage (reversible, needs the exact same key to read them back later);
    # this one SIGNS tokens (HMAC) so the server can verify a token wasn't forged/tampered with.
    # Different operations, different blast radius if leaked - keeping them as two independent secrets means rotating one
    # (e.g. to invalidate all outstanding tokens) doesn't also break every already-encrypted credential column, and vice versa.
    jwt_secret: str

    # --- Logging ---
    log_level: str          # 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
    log_file: str | None    # None means log to console only

    # --- Backfill ---
    # These paths are relative to the db_path directory.
    heartbeat_path: str
    backfill_status_path: str

    # --- Bootstrap admin (control DB - single-admin model) ---
    # Solves an otherwise-unavoidable chicken-and-egg problem: every account is created FROM an invite (control_db.schema.invites.created_by is NOT NULL),
    # and only an admin can create an invite - so without something outside that loop,
    # the very first account could never come into existence through the normal POST /auth/register flow at all
    # (scripts/manage_admin.py's `create` subcommand is the existing outside-the-loop answer, run by hand on the server;
    # this is a second one, reachable through the ordinary web UI instead of a shell).
    # Optional and unset by default (None): whoever stands up a fresh instance picks their own phrase and puts it in .env;
    # it works exactly once - see api/routes/auth.py's register() for how a matching invite_token is told apart from an ordinary invite
    # and turned into the one-and-only admin account (control_db.schema.ix_users_single_admin enforces there's never a second one) -
    # and does nothing at all once an admin already exists, regardless of whether the .env value is still set.
    # None (not empty-string) when unset, so register() can skip the comparison entirely for an instance that never configured one,
    # rather than a blank .env value ever accidentally matching an empty invite_token some other caller sent.
    bootstrap_admin_token: str | None

    # --- API documentation endpoints ---
    # Whether the interactive API docs (Swagger UI at /api/docs, ReDoc at /api/redoc) and the machine-readable schema (/api/openapi.json) are served.
    # OFF by default - secure by default: those pages are a complete, browsable map of every endpoint and its parameters,
    # which is exactly what a production instance shouldn't hand to anyone who asks (the endpoints stay protected either way; this is about not advertising them).
    # Set ENABLE_API_DOCS=true in your local .env while developing.
    enable_api_docs: bool


def _load() -> Settings:
    """
    Build the Settings instance from environment variables.
    Called once at module import time.
    """
    raw_api_id = _require("TG_API_ID")
    try:
        api_id = int(raw_api_id)
    except ValueError:
        sys.exit(
            f"[config] TG_API_ID must be an integer, got: {raw_api_id!r}"
        )

    log_file_raw = _optional("LOG_FILE", "")
    log_file = log_file_raw if log_file_raw else None

    bootstrap_admin_token_raw = _optional("TELEVAULT_BOOTSTRAP_ADMIN_TOKEN", "")
    bootstrap_admin_token = bootstrap_admin_token_raw if bootstrap_admin_token_raw else None

    # Accept the usual spellings of "yes"; anything else (including unset) is False.
    enable_api_docs = _optional("ENABLE_API_DOCS", "false").lower() in ("1", "true", "yes", "on")

    return Settings(
        api_id=                 api_id,
        api_hash=               _require("TG_API_HASH"),
        phone=                  _require("TG_PHONE"),
        session_name=           _optional("SESSION_NAME", "televault"),
        db_path=                _optional("DB_PATH", "data/televault.db"),
        database_url=           _optional("DATABASE_URL", "postgresql+psycopg://televault:televault@localhost:5432/televault"),
        control_database_url=   _optional("CONTROL_DATABASE_URL", "postgresql+psycopg://televault:televault@localhost:5432/televault_control"),
        fernet_key=             _require("FERNET_KEY"),
        jwt_secret=             _require("JWT_SECRET"),
        log_level=              _optional("LOG_LEVEL", "INFO"),
        log_file=               log_file,
        heartbeat_path=         _optional("HEARTBEAT_PATH", "data/televault.heartbeat"),
        backfill_status_path=   _optional("BACKFILL_STATUS_PATH", "data/backfill_status.json"),
        bootstrap_admin_token=  bootstrap_admin_token,
        enable_api_docs=        enable_api_docs,
    )


# The single shared instance.  Import this everywhere.
settings = _load()