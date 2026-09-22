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
    ✓ Whether THIS caller is the one account with a Telegram session at all (see is_instance_owner and session_exists' own notes below -
      there is exactly one physical Telethon session today, belonging to one account, not "the instance" in the abstract)
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

    `is_instance_owner` / `session_exists`: there is exactly one physical Telethon .session file per running instance today
    (see require_instance_owner()'s docstring in api/dependencies.py) - it belongs to whichever ONE account's archive_db_ref matches this instance's own database_url,
    the same "instance owner" concept require_instance_owner() checks.
    For every other account, a Telegram session genuinely doesn't exist yet - there's no per-user linking flow built yet (see project roadmap) -
    so session_exists is correctly False for them, not a stale/borrowed reading of someone else's session.
    `status` only folds session_exists into its ok/degraded computation FOR the instance owner:
    a regular account's overall status shouldn't read "degraded" forever over a check that was never theirs to pass in the first place.

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

    is_instance_owner = archive_ref is not None and archive_ref == db.get_primary_database_name()
    session_path = Path(settings.session_name).with_suffix(".session")
    session_exists = is_instance_owner and session_path.exists()

    db_readable = archive_status == "ok"
    if is_instance_owner:
        status = "ok" if (db_readable and session_exists) else "degraded"
    else:
        status = "ok" if db_readable else "degraded"

    return HealthOut(
        status=status,
        archive_status=archive_status,
        db_readable=db_readable,
        is_instance_owner=is_instance_owner,
        session_exists=session_exists,
        db_message_count=message_count,
    )