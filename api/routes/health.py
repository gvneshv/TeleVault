"""
GET /api/health — liveness check for the API server and the caller's own archive.

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
    ✓ Telethon .session file exists on disk (instance-level - see session_exists' own note below)
    ✗ Whether the userbot is currently connected to Telegram (that requires IPC — a Phase 3 addition, see CHANGELOG)
"""

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

import control_db
import db
from api.dependencies import get_control_db, get_current_user
from api.schemas import HealthOut
from config import settings
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
        - "ok"          the caller's archive was reached and read successfully (db_message_count is accurate, including 0 - an attached-but-empty archive is NOT "unavailable")
        - "unattached"  this account has no archive_db_ref yet - nothing to check
        - "unavailable" an archive_db_ref exists but couldn't actually be reached right now

    `status` stays 'ok'/'degraded' for backwards compatibility with the existing UI badge -
    'ok' requires both archive_status == "ok" and the Telethon session file being present.
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
        except OperationalError:
            archive_status = "unavailable"

    # Telethon session file: instance-level, not per-user - there is one physical userbot process per instance today
    # (see require_instance_owner()'s docstring in api/dependencies.py).
    # Presence means the userbot has authenticated at least once;
    # absence most likely means first-run setup hasn't completed.
    # Shown to every caller regardless of archive_status - it describes this instance's userbot process, not any individual account.
    session_path = Path(settings.session_name).with_suffix(".session")
    session_exists = session_path.exists()

    db_readable = archive_status == "ok"
    status = "ok" if (db_readable and session_exists) else "degraded"

    return HealthOut(
        status=status,
        archive_status=archive_status,
        db_readable=db_readable,
        session_exists=session_exists,
        db_message_count=message_count,
    )