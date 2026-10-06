"""
FastAPI application factory and lifespan manager for the TeleVault API.

Process topology reminder:
    The userbot (main.py) and this API server are two separate processes sharing one PostgreSQL database.
    The userbot writes; this server only reads.
    Never open a write connection here - use db.get_readonly_connection() from api/dependencies.py exclusively.
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
from starlette.exceptions import HTTPException as StarletteHTTPException

import control_db
import db
from api.dependencies import require_instance_owner, require_admin
from config import settings

from .routes import auth, chats, messages, deleted, stats, health, backfill, telethon, telegram, archive, admin

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

    Also starts/stops the CONTROL database's own, separate pool (control_db.init_control_db() control_db.close_db()) -
    see control_db/connection.py's module docstring for why it's a second, independent Engine rather than a second function bolted onto db.init_db().
    """
    logger.info("TeleVault API starting up.")
    db.init_db(settings.database_url)
    control_db.init_control_db(settings.control_database_url)
    yield
    control_db.close_db()
    db.close_tenant_engines()
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
    # Interactive docs + schema are opt-in (ENABLE_API_DOCS in .env, off by default - see config.Settings.enable_api_docs for why).
    # Passing None removes the route entirely: the paths then fall through to the static mount and get the same 404 as any other unknown /api/ path.
    docs_url="/api/docs" if settings.enable_api_docs else None,
    redoc_url="/api/redoc" if settings.enable_api_docs else None,
    openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# API routes - all prefixed with /api to allow Nginx to proxy them cleanly
#
# chats/messages/deleted/stats:
# each ROUTE (not the router as a whole) depends on # api.dependencies.get_archive_connection instead of get_db -
# it resolves the CALLING user's own archive (control_db.users.archive_db_ref) per request,
# so this is a per-user gate applied inside each route function's own Depends(),
# not a blanket router-level one - see that dependency's docstring for why a single shared gate here would be the wrong shape
# (there is no longer one instance-wide archive to gate as a whole).
#
# backfill/telethon:
# gated at the router level on require_instance_owner,
# since controlling this one running process's one live userbot is inherently single-owner regardless of how many control_db accounts exist -
# see that dependency's docstring for why it's deliberately a different question from get_archive_connection's, even though they resolve the same today.
#
# telegram / archive:
# each route depends on api.dependencies.get_current_user directly (like chats/messages/deleted/stats do via get_archive_connection),
# NOT require_instance_owner - linking Telegram and provisioning your own archive are both per-account self-service actions every user does for themselves,
# not the single-instance-owner action backfill/telethon are.
# See those routers' own module docstrings.
#
# admin:
# gated at the router level on require_admin - every route in api/routes/admin.py requires the same one thing (the caller is the sole admin),
# so a blanket router-level dependency is the right shape here, same reasoning as backfill/telethon's require_instance_owner just above.
# See that router's own module docstring.
#
# auth stays open: it's what issues the tokens everything else then checks.
# health requires a token (get_current_user) but is registered without a router-level dependency,
# same reasoning as chats/messages/deleted/stats above: it resolves the CALLING user's own archive per request,
# not a single instance-wide one - see health.py's own module docstring for why it moved off the open/no-token model it started with.
# ---------------------------------------------------------------------------

app.include_router(auth.router,      prefix="/api")
app.include_router(health.router,    prefix="/api")
app.include_router(chats.router,     prefix="/api")
app.include_router(messages.router,  prefix="/api")
app.include_router(deleted.router,   prefix="/api")
app.include_router(stats.router,     prefix="/api")
app.include_router(telegram.router,  prefix="/api")
app.include_router(archive.router,   prefix="/api")
app.include_router(backfill.router,  prefix="/api", dependencies=[Depends(require_instance_owner)])
app.include_router(telethon.router,  prefix="/api", dependencies=[Depends(require_instance_owner)])
app.include_router(admin.router,     prefix="/api", dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------------------
# Static files - web UI
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

class _WebStaticFiles(StaticFiles):
    """
    StaticFiles with one deliberate carve-out: unknown /api/... paths keep the plain JSON 404.

    With html=True, StaticFiles already serves web/404.html (with a real 404 status) for any path that matches no file -
    exactly what we want for a person mistyping a page URL, and why adding web/404.html is the whole "custom 404 page" feature.
    But this mount sits at "/", so it is also what catches a request for an API route that doesn't exist (a typo'd or removed endpoint).
    The frontend's apiFetch() and any script calling the API expect a JSON body there, not an HTML page -
    so for that namespace we skip the HTML fallback and raise the ordinary 404, which FastAPI renders as {"detail": "Not Found"} like before.

    `path` is relative to the mount point ("/"), so "/api/foo" arrives as "api/foo".
    """

    async def get_response(self, path, scope):
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


if _WEB_DIR.exists():
    # Mount at "/" so index.html is served at the root.
    # The API routes registered above take precedence because FastAPI matches them before falling through to the static mount.
    app.mount("/", _WebStaticFiles(directory=_WEB_DIR, html=True), name="web")
else:
    import logging
    logging.getLogger(__name__).warning(
        "web/ directory not found at %s - static UI will not be served. "
        "This is expected before the frontend is built.",
        _WEB_DIR,
    )