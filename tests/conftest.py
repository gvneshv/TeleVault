"""
Shared pytest setup for TeleVault.

Read this before changing anything here - the first half of this file exists to keep the test run from ever touching real data.

1. A hermetic environment, established BEFORE config.py is imported config.py freezes its settings at import time (and reads `.env` from the repo root).
   The first thing this file does, before any TeleVault module can be imported, is overwrite every setting the code reads with test values:
     - fresh FERNET_KEY / JWT_SECRET (so no test can decrypt, or forge a token for, a real instance),
     - CONTROL_DATABASE_URL pointing at a database that only exists for this run,
     - DATA_DIR pointing at a temporary directory (so no heartbeat/status file ever lands in the real `data/`).
   python-dotenv never overrides variables that are already set, so a developer's real `.env` cannot leak into the run.

2. Throwaway databases that can only ever have a test name Database tests need a real PostgreSQL
   (there is no meaningful fake for pg_trgm, triggers, partial unique indexes or `default_transaction_read_only`).
   They connect to the server in TELEVAULT_TEST_SERVER_URL (default: the docker-compose Postgres on localhost:5432)
   and create databases named `televault_test_<run id>_...`.
   Every drop goes through _drop_database(), which refuses any name without that prefix.
   Real databases (`televault_control`, `televault_archive_<id>`) can therefore never be created,
   migrated or dropped by this suite - not even when it shares the developer's own Postgres.
   Provisioning normally names archives `televault_archive_<id>`; the autouse fixture below patches that to the test prefix.

3. No side effects that leave the machine or spawn workers
   An autouse fixture blocks opening a real Telegram connection and launching main.py / backfill.py as subprocesses.
   Anything that tries raises AssertionError, so a test that would accidentally do either fails loudly instead of acting.

If no PostgreSQL is reachable, database tests are skipped with a message (set TELEVAULT_TEST_REQUIRE_DB=1 to make that an error, e.g. in CI).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# ---------------------------------------------------------------------------
# 1. Hermetic environment (must run before config.py is imported anywhere)
# ---------------------------------------------------------------------------

TEST_DB_PREFIX = "televault_test_"
RUN_ID = uuid.uuid4().hex[:8]
SERVER_URL = make_url(
    os.environ.get("TELEVAULT_TEST_SERVER_URL", "postgresql+psycopg://televault:televault@localhost:5432/postgres")
)
CONTROL_DB_NAME = f"{TEST_DB_PREFIX}{RUN_ID}_control"
BOOTSTRAP_TOKEN = "test-bootstrap-token-" + RUN_ID
_DATA_DIR = tempfile.mkdtemp(prefix="televault_test_data_")

os.environ.update(
    {
        "FERNET_KEY": Fernet.generate_key().decode(),
        "JWT_SECRET": ("test-jwt-secret-" + RUN_ID).ljust(40, "x"),  # >= 32 bytes, or PyJWT warns on every call
        "CONTROL_DATABASE_URL": SERVER_URL.set(database=CONTROL_DB_NAME).render_as_string(hide_password=False),
        "TELEVAULT_BOOTSTRAP_ADMIN_TOKEN": BOOTSTRAP_TOKEN,
        "DATA_DIR": _DATA_DIR,
        "LOG_FILE": "",
        "LOG_LEVEL": "INFO",
        "ENABLE_API_DOCS": "false",
    }
)


def archive_db_name(user_id: int) -> str:
    """The test-prefixed stand-in for db.provisioning._tenant_database_name()."""
    return f"{TEST_DB_PREFIX}{RUN_ID}_archive_{user_id}"


# ---------------------------------------------------------------------------
# 2. PostgreSQL availability + safe create/drop helpers
# ---------------------------------------------------------------------------


def _server_engine() -> sa.Engine:
    return sa.create_engine(SERVER_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool, connect_args={"connect_timeout": 3})


def _probe_postgres() -> str | None:
    """None if the test server is reachable, otherwise a short reason."""
    try:
        eng = _server_engine()
        with eng.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        eng.dispose()
        return None
    except Exception as exc:  # noqa: BLE001 - any failure just means "skip the DB tests"
        return f"{type(exc).__name__} connecting to {SERVER_URL.render_as_string(hide_password=True)}"


_PG_PROBLEM: str | None = None


def pytest_configure(config):
    global _PG_PROBLEM
    _PG_PROBLEM = _probe_postgres()


def pytest_collection_modifyitems(config, items):
    """Mark everything under tests/integration and tests/api as `db`, and skip those when no Postgres is reachable."""
    require = os.environ.get("TELEVAULT_TEST_REQUIRE_DB") == "1"
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "integration" in parts or "api" in parts:
            item.add_marker(pytest.mark.db)
        if item.get_closest_marker("db") and _PG_PROBLEM:
            if require:
                raise pytest.UsageError(f"TELEVAULT_TEST_REQUIRE_DB=1 but PostgreSQL is not reachable: {_PG_PROBLEM}")
            item.add_marker(pytest.mark.skip(reason=f"PostgreSQL not reachable ({_PG_PROBLEM}); see tests/README.md"))


def _drop_database(name: str) -> None:
    """DROP DATABASE, but only ever for names this suite created.
    The prefix check is the safety net - do not weaken it."""
    if not name.startswith(TEST_DB_PREFIX):
        raise RuntimeError(f"Refusing to drop {name!r}: not a test database (must start with {TEST_DB_PREFIX!r}).")
    eng = _server_engine()
    with eng.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


def _create_database(name: str) -> None:
    if not name.startswith(TEST_DB_PREFIX):
        raise RuntimeError(f"Refusing to create {name!r}: not a test database.")
    eng = _server_engine()
    with eng.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


def _list_run_databases() -> list[str]:
    eng = _server_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
            {"p": f"{TEST_DB_PREFIX}{RUN_ID}%"},
        ).all()
    eng.dispose()
    return [r[0] for r in rows]


@pytest.fixture(scope="session")
def control_database():
    """Create this run's control database, migrate it with the real Alembic chain, and drop everything created during the run at the end."""
    from alembic import command
    from alembic.config import Config

    _create_database(CONTROL_DB_NAME)
    cfg = Config(str(REPO_ROOT / "alembic_control.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic_control"))
    command.upgrade(cfg, "head")
    try:
        yield CONTROL_DB_NAME
    finally:
        import control_db
        import db

        for closer in (control_db.close_db, db.close_tenant_engines, db.close_server):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        for name in _list_run_databases():
            _drop_database(name)


@pytest.fixture
def control_engine(control_database):
    """A plain engine on the test control DB, with every table emptied first so each test starts from a blank instance."""
    from config import settings

    eng = sa.create_engine(settings.control_database_url, poolclass=NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("TRUNCATE users, invites, refresh_tokens, auth_audit_log RESTART IDENTITY CASCADE"))
    yield eng
    eng.dispose()


@pytest.fixture
def control_conn(control_engine):
    """A connection for calling control_db.queries functions directly."""
    with control_engine.connect() as conn:
        yield conn


@pytest.fixture
def client(control_engine):
    """A TestClient on the real app (lifespan included), against the blank test control DB."""
    from fastapi.testclient import TestClient

    from api.server import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _archive_names_are_test_names(monkeypatch):
    """Provisioning must never create `televault_archive_<id>` on a shared server - see this module's docstring, point 2."""
    import db.provisioning

    # The control tables are truncated with RESTART IDENTITY for every test, so user ids repeat from test to test. A per-test suffix keeps each
    # test's archives apart (in production ids never repeat, so the real naming needs no suffix). Tests must read the name from the API
    # response / users.archive_db_ref instead of recomputing it.
    suffix = uuid.uuid4().hex[:6]
    monkeypatch.setattr(db.provisioning, "_tenant_database_name", lambda user_id: f"{archive_db_name(user_id)}_{suffix}")


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Block the two things no test may ever do: talk to Telegram, or launch a worker process."""
    from telethon.client.telegrambaseclient import TelegramBaseClient

    async def _blocked_connect(self, *args, **kwargs):
        raise AssertionError("A test tried to open a real Telegram connection.")

    monkeypatch.setattr(TelegramBaseClient, "connect", _blocked_connect)

    real_popen = subprocess.Popen

    def _guarded_popen(args, *a, **k):
        flat = " ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args)
        if "main.py" in flat or "backfill.py" in flat:
            raise AssertionError(f"A test tried to launch a worker process: {flat}")
        return real_popen(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Archive databases
# ---------------------------------------------------------------------------

_ARCHIVE_TABLES = "chats, senders, backfill_runs, chat_migrations"  # CASCADE reaches messages/edits/deletions; chat_migrations has no FK, so it is listed


@pytest.fixture
def server_ready(control_database):
    """Tell db/connection.py which Postgres server the archives live on (the API lifespan does this in production)."""
    import db
    from config import settings

    db.init_server(settings.control_database_url)
    yield
    for closer in (db.close_tenant_engines, db.close_server):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="session")
def _shared_archive_name(control_database):
    """One really-provisioned archive (created by db.provisioning, the same code path production uses), reused by every test that needs a blank one."""
    import db
    from config import settings
    from db import provisioning

    db.init_server(settings.control_database_url)
    original = provisioning._tenant_database_name
    provisioning._tenant_database_name = archive_db_name  # session scope cannot use the function-scoped monkeypatch fixture
    try:
        return provisioning.provision_archive_database(900_001)
    finally:
        provisioning._tenant_database_name = original


@pytest.fixture
def archive(server_ready, _shared_archive_name):
    """A blank, fully migrated archive: yields (name, write connection).
    Tables are emptied before each test."""
    import db

    eng = sa.create_engine(db.server_database_url(_shared_archive_name), poolclass=NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {_ARCHIVE_TABLES} RESTART IDENTITY CASCADE"))
    with eng.connect() as conn:
        yield type("Archive", (), {"name": _shared_archive_name, "conn": conn})()
    eng.dispose()
