import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# alembic/env.py runs with the CWD wherever `alembic` was invoked from,
# which isn't guaranteed to be the repo root (e.g. VPS deploy scripts, a different working directory in someone's terminal).
# Add the repo root to sys.path explicitly so `import config` / `import db.schema` resolve regardless.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema import metadata  # noqa: E402  (see sys.path insert above)

# this is the Alembic Config object, which provides access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# Only the standalone `alembic ...` CLI should (re)configure logging.
# When db/provisioning.py runs a migration INSIDE the API process it sets config.attributes["configure_logger"] = False:
# alembic.ini's [logger_root] would otherwise replace the root logger's handlers and drop its level to WARNING,
# silently muting every INFO line the server logs from then on (latent until the server configured its own logging - see api/server.py's lifespan).
# disable_existing_loggers=False is kept as a second safeguard for any other in-process caller:
# fileConfig()'s default (True) would disable every logger that exists at that moment - api.*, db.*, even uvicorn's.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Point autogenerate at our actual schema (db/schema.py's Table objects) instead of alembic.ini's sqlalchemy.url placeholder / a separate models module
# - db/schema.py is the single source of truth for the schema shape (see its own module docstring).
target_metadata = metadata

# Which database to migrate - there is NO default, on purpose.
# Archive databases are per-account and normally created AND migrated automatically (db/provisioning.py, via POST /api/archive/provision),
# so a bare `alembic upgrade head` has no single "the archive" to target any more (and this file no longer imports config.py or needs a .env at all).
# The target comes from, in order:
#   1. TELEVAULT_ALEMBIC_URL_OVERRIDE - set (and cleared) only by db/provisioning.py,
#      which runs migrations from inside the already-running API process and has no other way to hand this file a target
#      (env.py runs as Alembic's own entry point, it can't receive a Python argument).
#   2. `-x url=...` on the command line, for the rare manual run, e.g.:
#          alembic -x url=postgresql+psycopg://user:pass@localhost:5432/televault_archive_1 upgrade head
#      (Needed e.g. to bring an archive created by an older version up to date by hand. Quote the URL if it contains special characters.)
_target_url = os.environ.get("TELEVAULT_ALEMBIC_URL_OVERRIDE") or context.get_x_argument(as_dictionary=True).get("url")
if not _target_url:
    raise SystemExit(
        "No target database given. Archive databases are per-account; pass one explicitly:\n"
        "  alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head"
    )
config.set_main_option("sqlalchemy.url", _target_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an Engine is acceptable here as well.
    By skipping the Engine creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()