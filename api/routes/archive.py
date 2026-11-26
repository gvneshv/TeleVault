"""
POST /archive/provision - automatic archive-database provisioning.
GET  /archive/owner     - which Telegram account this archive belongs to (so the UI can draw the owner's own messages on the right).
DELETE /archive/data    - forget everything stored (messages, chats, senders), keeping the owner's settings; see db.queries.clear_all_data.
GET/PUT /archive/retention, POST /archive/retention/preview - the "delete messages older than N months" policy (applied by retention.py in the archiver).

Turns the two manual steps every account previously needed after linking Telegram
(an admin running `CREATE DATABASE` and `alembic upgrade head` by hand, per the README, then scripts/manage_admin.py set-archive) into one self-service call.
See db/provisioning.py for the actual create-and-migrate logic - this route is a thin wrapper:
check the account doesn't already have an archive, call that module, record the result.

Gated by Depends(get_current_user) only, same reasoning as api/routes/telegram.py: every account provisions its OWN archive, this isn't an instance-owner-only action.

Not gated on having linked Telegram first (control_db.users.telegram_session_string being set) - provisioning an archive and linking Telegram are separable concerns,
and there's no correctness reason to force one order over the other;
the natural flow (frontend calls this right after POST /telegram/link/confirm returns linked=true) is a UI choice, not something this endpoint enforces.
"""

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import control_db
from api.dependencies import get_archive_connection, get_archive_write_connection, get_control_db, get_current_user
from api.process_utils import is_backfill_running
from api.schemas import (
    ArchiveClearedOut,
    ArchiveOwnerOut,
    ArchiveProvisionOut,
    RetentionIn,
    RetentionOut,
    RetentionPreviewIn,
    RetentionPreviewOut,
)
import db
from db.queries import clear_all_data, save_retention_policy
from db.read_queries import count_retention_candidates, get_archive_owner, get_retention_policy
from db.provisioning import ArchiveProvisioningError, provision_archive_database
from utils.archive_login import encrypt_archive_password
from utils.media_files import remove_media_files
from utils.retention_rules import preview_is_stale
from utils.security import DecodedAccessToken
from utils.worker_paths import backfill_status_path

router = APIRouter(prefix="/archive", tags=["archive"])


@router.post("/provision", response_model=ArchiveProvisionOut, summary="Create and migrate this account's own archive database")
def provision(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> ArchiveProvisionOut:
    """
    Create a fresh, fully-migrated archive database for this account and record it as its archive_db_ref, in one call.

    Refuses (409) if archive_db_ref is already set - deliberately not idempotent the way db/provisioning.py's own create-then-migrate steps are:
    silently reusing an existing reference here would hide a mistake (calling this twice by accident) rather than surface it,
    and there's no legitimate reason for an already-provisioned account to call this again.
    Someone who genuinely wants to re-provision (rare - e.g. starting over) still has scripts/manage_admin.py's set-archive,
    which supports overwriting with an explicit confirmation prompt, unlike this endpoint.
    """
    user = control_db.queries.get_user_by_id(control_conn, current["user_id"])
    if user is not None and user["archive_db_ref"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "This account already has an archive database.", "reason": "archive_already_provisioned"},
        )

    try:
        provisioned = provision_archive_database(current["user_id"])
    except ArchiveProvisioningError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "reason": exc.reason}) from exc

    # The database, its own role and that role's (encrypted) password are saved in ONE statement:
    # an account can never end up pointing at a database whose login was not stored.
    # The password is not part of the response or any log line - it exists only in this process's memory and, encrypted, in the control DB.
    control_db.queries.set_archive_db_ref(
        control_conn,
        current["user_id"],
        provisioned.db_name,
        provisioned.login.role,
        encrypt_archive_password(provisioned.login.password),
    )
    return ArchiveProvisionOut(archive_db_ref=provisioned.db_name)


@router.get("/owner", response_model=ArchiveOwnerOut, summary="Which Telegram account this archive belongs to")
def get_owner(conn: Connection = Depends(get_archive_connection)) -> ArchiveOwnerOut:
    """
    The Telegram user id the archiver recorded for this account, or `owner_id: null` when it is not known yet.

    The chat view compares it with each message's `sender.sender_id` to draw the owner's own messages on the right.
    Read-only (get_archive_connection).
    An id is not secret from its owner, and this only ever answers for the CALLING account's own archive.
    """
    return ArchiveOwnerOut(owner_id=get_archive_owner(conn))


# The literal the caller must send.
# The UI already makes the person type a phrase, so this is not authentication:
# it only stops a stray or mistyped request (a wrong URL, a script, a crawler following links) from emptying an archive.
# Missing or different -> 422, nothing is touched.
CLEAR_CONFIRMATION = "delete-all-data"


@router.delete("/data", response_model=ArchiveClearedOut, summary="Delete everything stored in this archive (cannot be undone)")
def clear_archive_data(
    confirm: str = Query(..., max_length=len(CLEAR_CONFIRMATION), description=f"Must be exactly '{CLEAR_CONFIRMATION}'."),
    current: DecodedAccessToken = Depends(get_current_user),
    conn: Connection = Depends(get_archive_write_connection),
) -> dict:
    """
    Empty the caller's archive: every message (with edit history and deletion snapshots),
    chat, group-migration record and backfill run, and every sender except those who carry a display name.
    Kept: the "chats not archived" blocklist, the display names and the archive-owner row.
    The caller is expected to have asked for confirmation first; there is no undo and no soft delete.
    The live archiver may keep running and stores new messages afterwards.
    This is not account deletion (GDPR erasure is a separate feature).

    Errors: 422 if `confirm` is not exactly "delete-all-data"; 409 "backfill_running" while this account's backfill runs
    (it would store the history again - try again when it has finished); 409 "archive_unattached" without an archive.
    Only the CALLER's own archive is reachable (get_archive_write_connection resolves it from the access token).
    """
    if confirm != CLEAR_CONFIRMATION:
        raise HTTPException(
            status_code=422,
            detail={"message": "Confirmation text is missing or wrong.", "reason": "confirmation_required"},
        )
    if is_backfill_running(backfill_status_path(current["user_id"])):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A backfill is running and would store the history again. Wait for it to finish (or cancel it), then try again.",
                "reason": "backfill_running",
            },
        )
    result = clear_all_data(conn)
    # The database part is committed; now every downloaded file of the removed messages goes too.
    # `media_paths` is dropped by the response model.
    remove_media_files(current["user_id"], result["media_paths"])
    return result


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------


@contextmanager
def _needs_retention_table():
    """Turn "this archive has no retention_policy table" into a clear 409 the UI can translate (reason "archive_outdated"). Other errors propagate."""
    try:
        yield
    except ProgrammingError as exc:
        if not db.is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database needs updating before automatic cleanup can be set. Run `python scripts/migrate_archives.py` - see RUNBOOK.md.",
                "reason": "archive_outdated",
            },
        ) from exc


@router.get("/retention", response_model=RetentionOut, summary="The automatic-cleanup policy and what its last run did")
def get_retention(conn: Connection = Depends(get_archive_connection)) -> RetentionOut:
    """
    The saved "delete messages older than N months" policy, or `configured: false` (off) when none was ever saved.
    409 "archive_outdated" on an archive that has not been migrated.
    """
    with _needs_retention_table():
        policy = get_retention_policy(conn)
    return RetentionOut(configured=False) if policy is None else RetentionOut(configured=True, **policy)


@router.post("/retention/preview", response_model=RetentionPreviewOut, summary="How many messages the policy would delete right now (deletes nothing)")
def preview_retention(body: RetentionPreviewIn, conn: Connection = Depends(get_archive_connection)) -> dict:
    """
    Count what a policy with these settings would delete if it ran now.
    Changes nothing; counts and dates only.
    The page shows this and sends the count back as `confirmed_messages` when the owner saves.
    """
    return count_retention_candidates(conn, body.months, body.keep_deleted)


@router.put("/retention", response_model=RetentionOut, summary="Save the automatic-cleanup policy")
def put_retention(body: RetentionIn, conn: Connection = Depends(get_archive_write_connection)) -> RetentionOut:
    """
    Save the policy (replacing the previous one).
    Deleting is irreversible, so switching it ON needs `confirmed_messages`: the number the preview showed.
    The server counts again; if clearly more would be deleted than was confirmed (the page was stale, or the settings changed after the preview) nothing is saved.
    Saving also releases a run that was blocked by the safety limit.

    The job itself runs inside the archiver (retention.py), so nothing is deleted by this request - at the earliest within a minute, and only while the archiver runs.

    422 "confirmation_required" - enabling without `confirmed_messages`
    409 "preview_outdated"      - more would be deleted than confirmed; the message carries the new count, preview again
    409 "archive_outdated"      - the archive has not been migrated yet
    """
    confirmed = None
    if body.enabled:
        if body.confirmed_messages is None:
            raise HTTPException(
                status_code=422,
                detail={"message": "Check the preview and confirm how many messages will be deleted.", "reason": "confirmation_required"},
            )
        with _needs_retention_table():
            actual = count_retention_candidates(conn, body.months, body.keep_deleted)["count"]
        if preview_is_stale(body.confirmed_messages, actual):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"The number of messages to delete is now {actual}, more than the {body.confirmed_messages} you confirmed. Preview again.",
                    "reason": "preview_outdated",
                    "actual": actual,
                },
            )
        confirmed = body.confirmed_messages
    with _needs_retention_table():
        save_retention_policy(conn, enabled=body.enabled, months=body.months, keep_deleted=body.keep_deleted, confirmed_messages=confirmed)
        policy = get_retention_policy(conn)
    return RetentionOut(configured=True, **(policy or {}))