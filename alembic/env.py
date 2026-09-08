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

from config import settings  # noqa: E402  (see sys.path insert above)
from db.schema import metadata  # noqa: E402

# this is the Alembic Config object, which provides access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Point autogenerate at our actual schema (db/schema.py's Table objects) instead of alembic.ini's sqlalchemy.url placeholder / a separate models module
# - db/schema.py is the single source of truth for the schema shape (see its own module docstring).
target_metadata = metadata

# The DB URL comes from TeleVault's own config (settings.database_url, itself sourced from .env's DATABASE_URL) rather than being duplicated into alembic.ini.
# This does mean `alembic` commands require a fully-populated .env
# (including the Telegram credentials config.py requires at import time)
# even though they have nothing to do with Telegram - an accepted coupling, not an oversight;
# see this migration's commit notes for why.
config.set_main_option("sqlalchemy.url", settings.database_url)


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