"""
FastAPI application factory and lifespan manager for the TeleVault API.

Process topology reminder:
    The userbot (main.py) and this API server are two separate processes sharing one PostgreSQL database.
    The userbot writes; this server only reads.
    Never open a write connection here — use db.get_readonly_connection() from api/dependencies.py exclusively.
    Exception:
    the CONTROL database (accounts/invites/refresh tokens/audit log, api/routes/auth.py) - this server is that database's only writer, by design.
    See control_db/connection.py's module docstring for the full reasoning; the rule above is about the ARCHIVE database specifically.

Running in development:
    uvicorn televault.api.server:app --reload --port 8000

On VPS (via systemd):
    ExecStart=uvicorn televault.api.server:app --host 127.0.0.1 --port 8000
    Nginx proxies /api/* to this process; /web/* is served directly by Nginx.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

import control_db
import db
from api.dependencies import require_owner
from config import settings

from .routes import auth, chats, messages, deleted, stats, health, backfill, telethon

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler (replaces the deprecated on_event pattern).

    Startup: creates the Postgres connection pool (db.init_db()) - this process is separate from main.py (the userbot),
    so main.py's own init_db() call never runs here.
    Without this, every request would fail with "Database not initialised" the moment a route's get_db() dependency tried
    to check out a connection - there's no implicit fallback.
    This is genuinely required now, unlike the old SQLite version (a raw sqlite3.connect() per request needed no prior setup at all).

    Shutdown: disposes the pool (db.close_db()) - closes every pooled connection cleanly rather than leaving them to the OS on process exit.

    Also starts/stops the CONTROL database's own, separate pool (control_db.init_control_db() / control_db.close_db()) -
    see control_db/connection.py's module docstring for why it's a second, independent Engine rather than a second function bolted onto db.init_db().
    """
    logger.info("TeleVault API starting up.")
    db.init_db(settings.database_url)
    control_db.init_control_db(settings.control_database_url)
    yield
    control_db.close_db()
    db.close_db()
    logger.info("TeleVault API shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TeleVault API",
    description=(
        "Read-only REST API for the TeleVault personal Telegram archive. "
        "All write operations are performed exclusively by the userbot process."
    ),
    version="2.0.0",
    # Disable the default /docs and /redoc in production by setting these to None.
    # Leave them enabled for now — useful during development.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# API routes — all prefixed with /api to allow Nginx to proxy them cleanly
#
# chats/messages/deleted/stats/backfill/telethon are gated on require_owner (api/dependencies.py):
# this instance's archive belongs to exactly one account (config.settings.owner_user_id),
# and only that account may read it or control its archiver/backfill - not "any logged-in user", not "any admin".
# auth and health stay open: auth issues the tokens require_owner then checks,
# and health is a liveness probe with no archive data in it (see health.py's own docstring).
# ---------------------------------------------------------------------------

app.include_router(auth.router,      prefix="/api")
app.include_router(health.router,    prefix="/api")
app.include_router(chats.router,     prefix="/api", dependencies=[Depends(require_owner)])
app.include_router(messages.router,  prefix="/api", dependencies=[Depends(require_owner)])
app.include_router(deleted.router,   prefix="/api", dependencies=[Depends(require_owner)])
app.include_router(stats.router,     prefix="/api", dependencies=[Depends(require_owner)])
app.include_router(backfill.router,  prefix="/api", dependencies=[Depends(require_owner)])
app.include_router(telethon.router,  prefix="/api", dependencies=[Depends(require_owner)])


# ---------------------------------------------------------------------------
# Static files — web UI
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

if _WEB_DIR.exists():
    # Mount at "/" so index.html is served at the root.
    # The API routes registered above take precedence because FastAPI matches them before falling through to StaticFiles.
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
else:
    import logging
    logging.getLogger(__name__).warning(
        "web/ directory not found at %s — static UI will not be served. "
        "This is expected before the frontend is built.",
        _WEB_DIR,
    )