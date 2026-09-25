"""
POST /archive/provision - automatic archive-database provisioning.

Turns the two manual steps every account previously needed after linking Telegram
(an admin running `CREATE DATABASE` and `alembic upgrade head` by hand, per the README, then scripts/manage_admin.py set-archive) into one self-service call.
See db/provisioning.py for the actual create-and-migrate logic - this route is a thin wrapper:
check the account doesn't already have an archive, call that module, record the result.

Gated by Depends(get_current_user) only, same reasoning as api/routes/telegram.py: every account provisions its OWN archive, this isn't an instance-owner-only action.

Not gated on having linked Telegram first (control_db.users.telegram_session_string being set) - provisioning an archive and linking Telegram are separable concerns,
and there's no correctness reason to force one order over the other;
the natural flow (frontend calls this right after POST /telegram/link/confirm returns linked=true) is a UI choice, not something this endpoint enforces.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.engine import Connection

import control_db
from api.dependencies import get_control_db, get_current_user
from api.schemas import ArchiveProvisionOut
from db.provisioning import ArchiveProvisioningError, provision_archive_database
from utils.security import DecodedAccessToken

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
        db_name = provision_archive_database(current["user_id"])
    except ArchiveProvisioningError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "reason": exc.reason}) from exc

    control_db.queries.set_archive_db_ref(control_conn, current["user_id"], db_name)
    return ArchiveProvisionOut(archive_db_ref=db_name)