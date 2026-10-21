"""
FastAPI application factory and lifespan manager for the TeleVault API.

Process topology reminder:
    Each account's userbot (main.py --user-id N) and this API server are separate processes; every account has its OWN archive database.
    The userbots write to the archives; this server only reads them (db.get_tenant_readonly_connection(), via api/dependencies.py's get_archive_connection()).
    Never open a write connection to an archive here.
    The only exception is cancel_backfill() marking a killed backfill's history row (see api/routes/backfill.py).
    Exception:
    the CONTROL database (accounts/invites/refresh tokens/audit log, api/routes/auth.py) - this server is that database's only writer, by design.
    See control_db/connection.py's module docstring for the full reasoning; the rule above is about the ARCHIVE databases specifically.

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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import control_db
import db
from api.dependencies import require_admin
from config import settings
from control_db import queries as cq
from utils.logging_setup import setup_logging

from .routes import auth, chats, messages, deleted, stats, health, backfill, telethon, telegram, archive, admin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def _ensure_first_admin_is_possible() -> None:
    """
    Refuse to start an instance on which nobody could ever become the admin.

    The dead end this guards against:
        every account is created from an invite, only the admin can create invites, and the admin account itself can only come into existence through one of two doors -
        TELEVAULT_BOOTSTRAP_ADMIN_TOKEN (register through the web UI with that phrase) or `scripts/manage_admin.py create` (a shell on the server).
        With NO admin yet and NEITHER door available, the registration page would simply reject every invite token forever, and nothing in the UI would say why.
        Failing at startup, with both remedies spelled out, turns that silent dead end into a one-line fix.

    What this deliberately does NOT do:
        - It does not require the token on every start.
          Once an admin exists the token is inert (see api/routes/auth.py's _is_bootstrap_admin_token),
          so demanding it forever would force a pointless secret into .env and into every process that imports config.py (Alembic, the workers).
          The check is satisfied by EITHER an existing admin OR a configured token.
        - It does not block startup when the question can't be answered (control database unreachable, or its schema not migrated yet).
          Those problems already surface with their own, more specific errors on the first real request,
          and refusing to boot here would hide them behind a misleading "no admin" message.
          We log a warning and carry on.

    Raises:
        RuntimeError: no admin exists and no bootstrap token is configured. uvicorn reports it as "Application startup failed" and exits.
    """
    try:
        conn = control_db.get_connection()
        try:
            has_admin = cq.admin_exists(conn)
        finally:
            conn.close()
    except Exception as exc:
        # One line, no traceback: "database not up yet" is an ordinary condition here, and the real error will surface on the first request.
        logger.warning(
            "Could not check whether an admin account exists (%s) - is the control database up and migrated "
            "(`alembic -c alembic_control.ini upgrade head`)? Skipping the first-admin check.",
            type(exc).__name__,
        )
        return

    if has_admin or settings.bootstrap_admin_token is not None:
        return

    raise RuntimeError(
        "No admin account exists yet and TELEVAULT_BOOTSTRAP_ADMIN_TOKEN is not set, so nobody would be able to register.\n"
        "Fix it in one of two ways, then start the server again:\n"
        "  1. Put a random phrase in .env (e.g. TELEVAULT_BOOTSTRAP_ADMIN_TOKEN=<output of `openssl rand -hex 16`>),\n"
        "     then register in the web UI using that phrase as the invite token - you become the admin.\n"
        "  2. Or create the admin from a shell: python scripts/manage_admin.py create --username YOURNAME"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler (replaces the deprecated on_event pattern).

    Startup:
      - control_db.init_control_db(): the control database's pool (accounts, invites, tokens, audit log, encrypted Telegram credentials).
      - db.init_server(): tells db/connection.py which Postgres server the per-account archives live on (the control URL is the template -
        same server/credentials, different database name).
        Per-account archive engines are then created lazily, on each account's first request.
      - _ensure_first_admin_is_possible(): aborts startup if no admin exists and no bootstrap token is configured (see that function).
    There is no single "primary" archive to open here any more: this process serves every account, and each request resolves the caller's own archive.
    Without init_server(), the first request touching an archive (or provisioning one) would fail with "Database server not configured".

    Shutdown: disposes the control pool, every cached per-account archive pool, and the provisioning (CREATE/DROP DATABASE) connection -
    closes every pooled connection cleanly rather than leaving them to the OS on process exit.
    """
    # uvicorn configures only ITS OWN loggers ("uvicorn", "uvicorn.access");
    # the root logger stays at Python's default, WARNING, so every INFO line from our modules (api.*, db.*, control_db.*) was silently dropped until this call.
    # The userbot workers (main.py, backfill.py) always did this; the API server simply never had. Same LOG_LEVEL / LOG_FILE settings as the workers.
    # basicConfig() inside is a no-op if the root logger already has handlers (e.g. under pytest), so calling it here can never double-print.
    setup_logging(log_level=settings.log_level, log_file=settings.log_file)
    logger.info("TeleVault API starting up.")
    control_db.init_control_db(settings.control_database_url)
    db.init_server(settings.control_database_url)
    try:
        _ensure_first_admin_is_possible()
    except RuntimeError:
        # Startup is being aborted, so the shutdown code below never runs - release the pools we just opened before propagating.
        control_db.close_db()
        db.close_server()
        raise
    yield
    control_db.close_db()
    db.close_tenant_engines()
    db.close_server()
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
    version="3.0.0",
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
# each ROUTE (not the router as a whole) depends on api.dependencies.get_archive_connection -
# it resolves the CALLING user's own archive (control_db.users.archive_db_ref) per request,
# so this is a per-user gate applied inside each route function's own Depends(),
# not a blanket router-level one - see that dependency's docstring for why a single shared gate here would be the wrong shape
# (there is no longer one instance-wide archive to gate as a whole).
#
# backfill/telethon:
# per-account, like everything else: each route acts on the CALLING account's own worker process, status and history.
# Starting one additionally requires finished setup (api.dependencies.require_archiving_ready - Telegram linked + archive provisioned);
# status/stop/cancel only need a valid login.
# There is no router-level gate - see those routers' own module docstrings.
#
# telegram / archive:
# each route depends on api.dependencies.get_current_user directly (like chats/messages/deleted/stats do via get_archive_connection) -
# linking Telegram and provisioning your own archive are per-account self-service actions every user does for themselves.
# See those routers' own module docstrings.
#
# admin:
# gated at the router level on require_admin - every route in api/routes/admin.py requires the same one thing (the caller is the sole admin),
# so a blanket router-level dependency is the right shape here (unlike backfill/telethon, whose routes are per-account and gated individually).
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
app.include_router(backfill.router,  prefix="/api")
app.include_router(telethon.router,  prefix="/api")
app.include_router(admin.router,     prefix="/api", dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------------------
# Static files - web UI
# ---------------------------------------------------------------------------

_log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Where the instance operator keeps their FILLED-IN legal pages (git-ignored - see .gitignore and the README's "Terms of Service and Privacy Policy" note).
# The tracked web/tos.html and web/privacy.html are placeholder templates that every clone ships with;
# the operator's real name/contact/jurisdiction live only here, on the server, and never reach the public repository.
_INSTANCE_LEGAL_DIR = Path(__file__).resolve().parent.parent / "instance" / "legal"


def _legal_page_route(slug: str):
    """
    Serve the Terms/Privacy page at the clean URL /<slug> (no ".html").

    StaticFiles only maps a URL to an existing file name, so /tos would 404 even though /tos.html works -
    and the legal pages are linked (and quoted in the Terms/Privacy text itself) as /tos and /privacy.
    The ".html" URLs keep working through the static mount as well; this just adds the clean ones.

    Which file is served is decided PER REQUEST:
    instance/legal/<slug>.html (the operator's filled-in copy) if it exists, otherwise the tracked placeholder template in web/.
    Deciding per request means dropping the file onto a running server just works, no restart.
    """
    template = _WEB_DIR / f"{slug}.html"

    @app.get(f"/{slug}", include_in_schema=False)
    def _serve() -> FileResponse:
        instance_copy = _INSTANCE_LEGAL_DIR / f"{slug}.html"
        return FileResponse(instance_copy if instance_copy.is_file() else template, media_type="text/html")

    return _serve


class _WebStaticFiles(StaticFiles):
    """
    StaticFiles with one deliberate carve-out: unknown /api/... paths keep the plain JSON 404.

    With html=True, StaticFiles already serves web/404.html (with a real 404 status) for any path that matches no file -
    exactly what we want for a person mistyping a page URL, and why adding web/404.html is the whole "custom 404 page" feature.
    But this mount sits at "/", so it is also what catches a request for an API route that doesn't exist (a typo'd or removed endpoint).
    The frontend's apiFetch() and any script calling the API expect a JSON body there, not an HTML page -
    so for that namespace we skip the HTML fallback and raise the ordinary 404, which FastAPI renders as {"detail": "Not Found"} like before.

    `path` is relative to the mount point ("/"), so "/api/foo" arrives as "api/foo" - on POSIX.
    Starlette builds it with os.path.normpath(), so on WINDOWS the same request arrives as "api\\foo".
    Comparing against "api/" alone silently never matched there, and every unknown /api/ path got the HTML 404 page
    (found by tests/api/test_gate_chain.py when the suite was first run on Windows).
    Normalise the separator before comparing.
    """

    async def get_response(self, path, scope):
        url_style = path.replace("\\", "/")  # compare on a copy; the original goes to StaticFiles untouched
        if url_style == "api" or url_style.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


if _WEB_DIR.exists():
    # Registered BEFORE the "/" mount below - routes added earlier win, and the mount would otherwise swallow these paths first.
    for _slug in ("tos", "privacy"):
        _legal_page_route(_slug)
        if not (_INSTANCE_LEGAL_DIR / f"{_slug}.html").is_file():
            # Not an error (a fresh clone or a dev machine legitimately has none), but a real deployment must not run on the placeholder text.
            _log.warning(
                "Serving the PLACEHOLDER template for /%s. Before going live, copy web/%s.html to %s and fill in its [PLACEHOLDER] fields (no restart needed afterwards).",
                _slug, _slug, _INSTANCE_LEGAL_DIR / f"{_slug}.html",
            )

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