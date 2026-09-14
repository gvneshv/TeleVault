"""
Alembic environment for the CONTROL database (users/invites/refresh_tokens/auth_audit_log - control_db/schema.py).

This is a deliberately separate Alembic setup from alembic/env.py
(which manages db/schema.py's archive-schema metadata) rather than one setup managing two MetaData objects in a single database:

  - The two schemas live in genuinely different databases
    (settings.control_database_url vs settings.database_url - see control_db/schema.py's module docstring for why),
    and a single Alembic "online" run only ever connects to one database at a time.
    Two databases means two independent revision histories,
    which is exactly what two Alembic setups (each with its own alembic_version table, in its own database) give for free.
  - Running `alembic upgrade head` against the wrong database is a real risk once two schemas/databases exist in one repo;
    two separate config files (alembic.ini vs alembic_control.ini, invoked as `alembic -c alembic_control.ini ...`)
    make "which database am I about to touch" an explicit choice on every command instead of an implicit default.

Everything below mirrors alembic/env.py's structure/reasoning exactly,
with metadata and the settings attribute swapped for the control DB's - see that file for the fuller rationale on each piece
(offline/online mode, sys.path handling, etc.), not repeated here.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# alembic_control/env.py runs with the CWD wherever `alembic` was invoked from, same caveat as alembic/env.py -
# add the repo root to sys.path so `import config` / `import control_db.schema` resolve regardless.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402  (see sys.path insert above)
from control_db.schema import metadata  # noqa: E402

# this is the Alembic Config object, which provides access to the values within the .ini file in use
# (alembic_control.ini, when invoked as `alembic -c alembic_control.ini ...`).
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Point autogenerate at control_db/schema.py's Table objects - the control DB's single source of truth for
# schema shape, same relationship db/schema.py has to alembic/env.py.
target_metadata = metadata

# settings.control_database_url (from .env's CONTROL_DATABASE_URL), NOT settings.database_url -
# this is the one line that actually determines which database this Alembic setup ever touches.
config.set_main_option("sqlalchemy.url", settings.control_database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode. See alembic/env.py's identical function for the full rationale."""
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
    """Run migrations in 'online' mode. See alembic/env.py's identical function for the full rationale."""
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