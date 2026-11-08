"""
POST /archive/provision - automatic archive-database provisioning.
GET  /archive/owner     - which Telegram account this archive belongs to (so the UI can draw the owner's own messages on the right).
DELETE /archive/data    - forget everything stored (messages, chats, senders), keeping the owner's settings; see db.queries.clear_all_data.

Turns the two manual steps every account previously needed after linking Telegram
(an admin running `CREATE DATABASE` and `alembic upgrade head` by hand, per the README, then scripts/manage_admin.py set-archive) into one self-service call.
See db/provisioning.py for the actual create-and-migrate logic - this route is a thin wrapper:
check the account doesn't already have an archive, call that module, record the result.

Gated by Depends(get_current_user) only, same reasoning as api/routes/telegram.py: every account provisions its OWN archive, this isn't an instance-owner-only action.

Not gated on having linked Telegram first (control_db.users.telegram_session_string being set) - provisioning an archive and linking Telegram are separable concerns,
and there's no correctness reason to force one order over the other;
the natural flow (frontend calls this right after POST /telegram/link/confirm returns linked=true) is a UI choice, not something this endpoint enforces.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.engine import Connection

import control_db
from api.dependencies import get_archive_connection, get_archive_write_connection, get_control_db, get_current_user
from api.process_utils import is_backfill_running
from api.schemas import ArchiveClearedOut, ArchiveOwnerOut, ArchiveProvisionOut
from db.queries import clear_all_data
from db.read_queries import get_archive_owner
from db.provisioning import ArchiveProvisioningError, provision_archive_database
from utils.archive_login import encrypt_archive_password
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
    return clear_all_data(conn)