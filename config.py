"""
Loads all runtime configuration from environment variables (via a .env file).
 
The settings are exposed as a single frozen dataclass instance - `settings` - imported directly wherever needed:
 
    from config import settings
    print(settings.control_database_url)
 
Why a dataclass rather than reading os.environ inline?
  - One import gives you everything; no hunting for os.getenv() calls scattered around the codebase.
  - The frozen=True flag prevents accidental mutation after startup.
  - Type annotations document what each setting is supposed to be.
  - Missing required values fail loudly at startup, not halfway through a run.
 
Required .env keys:       FERNET_KEY, JWT_SECRET
Optional (have defaults): CONTROL_DATABASE_URL, DATA_DIR, LOG_LEVEL, LOG_FILE, ENABLE_API_DOCS
Optional (no default - None means "not configured"): TELEVAULT_BOOTSTRAP_ADMIN_TOKEN (see Settings.bootstrap_admin_token below)

What is deliberately NOT here any more: Telegram credentials (api_id / api_hash / phone / a .session file).
TeleVault is multi-account - every account supplies its OWN api_id/api_hash and signs in to Telegram through the web UI,
and those values live encrypted in the control database (users.telegram_*), never in .env.
The per-account archive databases are derived from CONTROL_DATABASE_URL (same server and credentials, different database name) -
there is no separate "archive" connection string to configure. See utils/account.py for how a worker process loads one account's credentials.
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

    # --- Storage (control DB) ---
    # The single shared database holding accounts, invites, refresh tokens, the audit log and - encrypted - every account's Telegram credentials and session.
    # See control_db/schema.py's module docstring for why this is its own database.
    #
    # It also doubles as the "which Postgres server are the per-account archives on" setting:
    # every account's archive is a database on the SAME server with the SAME credentials, only the database name differs
    # (db.provisioning creates them as televault_archive_<user_id>; see db/connection.py's per-tenant section).
    # So there is deliberately no second connection string for archives.
    control_database_url: str

    # --- Runtime files (per-account heartbeat + backfill status) ---
    # Root directory for the small files the API server and the worker processes use to see each other
    # (see utils/worker_paths.py - each account gets its own subdirectory under here).
    # Resolved to an ABSOLUTE path at load time, so the API server and the workers it spawns agree on it regardless of which directory each was started from.
    data_dir: str

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
    log_file_raw = _optional("LOG_FILE", "")
    log_file = log_file_raw if log_file_raw else None

    bootstrap_admin_token_raw = _optional("TELEVAULT_BOOTSTRAP_ADMIN_TOKEN", "")
    bootstrap_admin_token = bootstrap_admin_token_raw if bootstrap_admin_token_raw else None

    # Accept the usual spellings of "yes"; anything else (including unset) is False.
    enable_api_docs = _optional("ENABLE_API_DOCS", "false").lower() in ("1", "true", "yes", "on")

    # A relative DATA_DIR is anchored to the repo root (this file's directory), NOT the current working directory:
    # the API server and every worker it spawns must resolve the same folder even if one was launched from somewhere else.
    data_dir = Path(_optional("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = Path(__file__).parent / data_dir

    return Settings(
        control_database_url=   _optional("CONTROL_DATABASE_URL", "postgresql+psycopg://televault:televault@localhost:5432/televault_control"),
        data_dir=               str(data_dir.resolve()),
        fernet_key=             _require("FERNET_KEY"),
        jwt_secret=             _require("JWT_SECRET"),
        log_level=              _optional("LOG_LEVEL", "INFO"),
        log_file=               log_file,
        bootstrap_admin_token=  bootstrap_admin_token,
        enable_api_docs=        enable_api_docs,
    )


# The single shared instance.  Import this everywhere.
settings = _load()