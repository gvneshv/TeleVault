"""
GET /api/health - liveness check for the API server and the caller's own archive.

Used by:
    - The web UI to show a status indicator (per-account, like /chats, /messages, /deleted, /stats)
    - Future: heartbeat from the userbot process (Phase 3)

Requires a valid access token, same as every other archive-facing route (see api/dependencies.py's get_current_user()).
This is NOT how the route started: it was originally open (no token required) back when there was exactly one archive per running instance and "is the process alive"
was a meaningful question on its own.
Now that every account has its own, potentially-nonexistent archive_db_ref, "is THE database healthy" isn't a question with a single answer any more -
there's no database to check without first knowing which caller is asking.
A monitoring script that wants a pure liveness probe with no archive semantics should hit something that doesn't need a token
(e.g. the process simply being up and answering HTTP at all) rather than this route - see CHANGELOG for whether/when a dedicated one gets added.

What this checks vs. what it doesn't:
    ✓ The caller's own archive database (resolved via control_db.users.archive_db_ref, same lookup get_archive_connection() uses for /chats, /messages, /deleted, /stats)
      is readable and returns rows
    ✓ Whether THIS caller has a Telegram session saved (session_exists - see its note in health_check()'s docstring)
    ✗ Whether the userbot is currently connected to Telegram (that requires IPC - a Phase 3 addition, see CHANGELOG)
"""

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import control_db
import db
from api.dependencies import get_control_db, get_current_user
from api.schemas import HealthOut
from utils.security import DecodedAccessToken

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut, summary="API and caller's archive liveness")
def health_check(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> HealthOut:
    """
    Return liveness status for the API and the caller's own archive database.

    `archive_status` is one of:
        - "ok"            the caller's archive was reached and read successfully
                          (db_message_count is accurate, including 0 - an attached-but-empty archive is NOT "unavailable")
        - "unattached"    this account has no archive_db_ref yet - nothing to check
        - "unavailable"   an archive_db_ref exists but couldn't actually be reached right now
                          (a transient outage - Postgres restarted, a network blip - that will clear up on its own;
                          "try again shortly" is genuinely correct advice here)
        - "misconfigured" an archive_db_ref exists but names a database that was never actually created
                          (see db.is_missing_database_error()'s own docstring for how this is told apart from "unavailable" above) -
                          a permanent admin-fix-it situation that will NEVER resolve by retrying,
                          so it gets its own status rather than being told apart from "unavailable" only in prose

    `session_exists`: whether this account has a Telegram session saved (signed in through the web UI - see api/routes/telegram.py).
    It does NOT mean the archiver is currently connected to Telegram - that is GET /api/telethon/status's job.

    A 200 response is always returned once the caller is authenticated - the body carries the real state, not the HTTP status,
    so partial-health states (e.g. "unattached") render as a normal page state in the UI rather than an error.
    (An invalid/expired/missing token itself still 401s, via get_current_user() above, same as every other authenticated route.)
    """
    message_count: int | None = None

    # Same lookup get_archive_connection() does for /chats, /messages, /deleted, /stats, but returning a status instead of raising 409/503:
    # this is a dashboard view, not a data endpoint, so "not set up yet" is a state to display, not an error.
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    archive_ref = user["archive_db_ref"] if user else None

    if archive_ref is None:
        archive_status = "unattached"
    else:
        try:
            with db.get_tenant_readonly_connection(archive_ref) as tenant_conn:
                message_count = db.get_message_count(tenant_conn)
            archive_status = "ok"
        except OperationalError as exc:
            if db.is_missing_database_error(exc):
                archive_status = "misconfigured"
            else:
                archive_status = "unavailable"

    # Whether the account has signed in to Telegram at all (control_db users.telegram_session_string - saved by api/routes/telegram.py's confirm()).
    # Reads the control DB row already fetched above; nothing on disk is consulted (there is no shared .session file any more).
    session_exists = bool(user and user["telegram_session_string"])

    db_readable = archive_status == "ok"
    status = "ok" if (db_readable and session_exists) else "degraded"

    return HealthOut(
        status=status,
        archive_status=archive_status,
        db_readable=db_readable,
        session_exists=session_exists,
        db_message_count=message_count,
    )