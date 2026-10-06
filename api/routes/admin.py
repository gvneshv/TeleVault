"""
Admin-only account management: list users, lock/unlock, delete, and create/list invites.

Everything here brings an existing scripts/manage_admin.py capability into the HTTP API/web UI (per the Decisions Log) EXCEPT the two things that stay CLI-only,
by design, forever:
  - `create` (bootstrapping the very first admin without an existing one to have invited them) -
    superseded for ordinary setup by the bootstrap-admin-token path in api/routes/auth.py's register(),
    but kept as a shell-access fallback (see that script's own module docstring).
  - `delete-admin` (the sole admin removing their OWN account) - deliberately never reachable through this router, or any other, no matter who's asking:
    see that subcommand's own docstring for why "an admin cannot delete themselves" has to be enforced by not building the door at all, not by a check inside one.
  - `set-archive` (manually pointing an account at an already-existing, already-migrated database) stays a rare,
    manual fallback for when automatic provisioning (db/provisioning.py, POST /archive/provision) isn't applicable -
    there's no reason an admin UI needs a button for what's meant to be an edge-case recovery tool.

Every route here is gated at the ROUTER level (api/server.py: `dependencies=[Depends(require_admin)]`),
not per-route - unlike telegram.py/archive.py (every route self-service, gated on get_current_user only) or chats.py/messages.py/etc.
(gated per-route via get_archive_connection, since each resolves a DIFFERENT per-caller archive).
Every single route in THIS router requires the same one thing (the caller is the admin),
so a blanket router-level dependency is the right shape here - same reasoning backfill.py/telethon.py already use for require_instance_owner.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.engine import Connection

import control_db
from api.dependencies import get_control_db, get_current_user
from api.process_utils import running_process_reason
from api.schemas import (
    AdminActionOut,
    AdminInviteCreateIn,
    AdminInviteListItemOut,
    AdminInviteListOut,
    AdminInviteOut,
    AdminUserDeleteOut,
    AdminUserListOut,
    AdminUserOut,
)
from config import settings
from db.deprovisioning import drop_archive_database
from utils.security import DecodedAccessToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _to_admin_user_out(row: dict) -> AdminUserOut:
    """Shared mapping from a raw control_db.queries.list_users()/get_user_by_id() row to the public shape -
    see api/schemas/admin.py's AdminUserOut docstring for why this never forwards password_hash or a live Telegram credential value, only whether one is set."""
    return AdminUserOut(
        id=row["id"],
        username=row["username"],
        is_admin=row["is_admin"],
        is_locked=row["is_locked"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        has_telegram_session=row["telegram_session_string"] is not None,
        has_archive=row["archive_db_ref"] is not None,
    )


@router.get("/users", response_model=AdminUserListOut, summary="List every account")
def list_users(control_conn: Connection = Depends(get_control_db)) -> AdminUserListOut:
    rows = control_db.queries.list_users(control_conn)
    return AdminUserListOut(users=[_to_admin_user_out(row) for row in rows])


@router.post("/users/{user_id}/lock", response_model=AdminActionOut, summary="Lock an account")
def lock_user(
    user_id: int,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> AdminActionOut:
    """
    Lock the target account (control_db.queries.lock_user() - see its own docstring for exactly what this does and doesn't touch).
    Reversible only by POST /users/{user_id}/unlock below - there is no automatic expiry.

    Refuses (400) to lock the caller's own account: the sole admin locking themselves out has no unlock path
    (unlocking is itself an admin-only action - see unlock_user() below) other than a shell script bypassing the API entirely,
    which is exactly the kind of self-inflicted lockout this check exists to prevent, not to merely make awkward.
    Does NOT need an "is this an admin?" check beyond that - the single-admin model
    (control_db.schema.ix_users_single_admin) means the only OTHER account that could ever be an admin is the caller's own, already covered by the self-lock check above.
    """
    if user_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot lock your own account.")
    if not control_db.queries.lock_user(control_conn, user_id, actor_id=current["user_id"]):
        raise HTTPException(status_code=404, detail="No such user.")
    return AdminActionOut()


@router.post("/users/{user_id}/unlock", response_model=AdminActionOut, summary="Unlock an account")
def unlock_user(
    user_id: int,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> AdminActionOut:
    if not control_db.queries.unlock_user(control_conn, user_id, actor_id=current["user_id"]):
        raise HTTPException(status_code=404, detail="No such user.")
    return AdminActionOut()


@router.delete("/users/{user_id}", response_model=AdminUserDeleteOut, summary="Permanently delete an account")
def delete_user(
    user_id: int,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> AdminUserDeleteOut:
    """
    Irreversibly delete the target account: the control_db row and everything with it (see control_db.queries.delete_user_completely()'s own docstring),
    then the account's archive database itself, if it had one.
    The frontend is responsible for making the person type/confirm something before ever reaching this endpoint -
    this route itself asks nothing and assumes the confirmation already happened, same posture control_db.queries.delete_user_completely() takes toward ITS callers.

    Refuses (400) to delete the caller's own account or any other admin's -
    see this module's own docstring for why self/admin deletion is scripts/manage_admin.py's delete-admin subcommand ONLY,
    never an HTTP endpoint, regardless of who's asking.
    Under the single-admin model this is really one check (there is no "other admin" to delete),
    but both conditions are checked explicitly rather than collapsed into one, so the 400 message is specific about which rule was hit.

    Refuses (409, reusing the SAME "already_running"/"backfill_running" reasons start_archiver()'s own guards use) while the live archiver or a backfill is running -
    see api.process_utils.running_process_reason()'s own docstring for why.
    Checked AFTER the target lookup/is_admin check above, not before:
    there's no reason to make someone stop the archiver first only to then discover their target was never a valid one anyway.

    Looks the target up FIRST (before calling delete_user_completely(),
    which would 404 via a None return - see that function's own docstring) specifically so is_admin can be checked before attempting the delete,
    and so the username is available for the response even though the row won't exist anymore by the time this returns.
    """
    if user_id == current["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account here. See scripts/manage_admin.py's delete-admin subcommand.")

    target = control_db.queries.get_user_by_id(control_conn, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user.")
    if target["is_admin"]:
        raise HTTPException(status_code=400, detail="Admin accounts cannot be deleted here. See scripts/manage_admin.py's delete-admin subcommand.")

    # See api.process_utils.running_process_reason()'s own docstring for why this checks instance-wide rather
    # than trying to work out whether the archiver/backfill happens to belong to THIS target account.
    reason = running_process_reason(settings.heartbeat_path, settings.backfill_status_path)
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Stop the userbot and any running backfill before deleting an account.",
                "reason": reason,
            },
        )

    try:
        archive_db_ref = control_db.queries.delete_user_completely(control_conn, user_id, actor_id=current["user_id"])
    except ValueError as exc:
        # Only ever raised for an account that created invites - impossible for a non-admin target given the is_admin check above,
        # but delete_user_completely() itself doesn't know that;
        # surfaced as a 409 ("conflict with existing state") rather than a 400, since it's about data that exists, not a bad request.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if archive_db_ref is not None:
        # Best-effort: the control_db side is already fully committed by this point
        # (see delete_user_completely()'s own docstring for why archive teardown deliberately happens second and separately) -
        # a failure here leaves an orphaned-but-harmless database behind, not an inconsistent account,
        # and is logged rather than turned into a 5xx for an operation the caller would otherwise have every reason to believe already succeeded.
        try:
            drop_archive_database(archive_db_ref)
        except Exception:
            logger.exception("Deleted user %r (id=%s) but failed to drop archive database %r - orphaned, needs manual cleanup.", target["username"], user_id, archive_db_ref)

    return AdminUserDeleteOut(username=target["username"])


@router.post("/invites", response_model=AdminInviteOut, summary="Create a single-use invite token")
def create_invite(
    body: AdminInviteCreateIn,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> AdminInviteOut:
    """
    HTTP equivalent of scripts/manage_admin.py's create-invite subcommand, for the caller's own admin id -
    unlike that script (which takes --created-by / --id because it has no logged-in caller to infer one from),
    there's exactly one admin who could possibly be calling this, so there's nothing to ask for.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expires_hours)
    token = control_db.queries.create_invite(control_conn, current["user_id"], expires_at)
    return AdminInviteOut(token=token, expires_at=expires_at)


@router.delete("/invites/{invite_id}", response_model=AdminActionOut, summary="Delete an unused invite")
def delete_invite(
    invite_id: int,
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
) -> AdminActionOut:
    """
    Delete an invite that has not been redeemed (see control_db.queries.delete_unused_invite() for exactly why only unused ones, and why no "revive").
    Doubles as revocation for a still-valid token: the row is gone, so registration with it fails immediately.

    404 for an unknown id;
    409 for an already-used invite (conflicts with existing state - it is kept as registration history).
    """
    try:
        deleted = control_db.queries.delete_unused_invite(control_conn, invite_id, actor_id=current["user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="No such invite.")
    return AdminActionOut()


@router.get("/invites", response_model=AdminInviteListOut, summary="List every invite ever created")
def list_invites(control_conn: Connection = Depends(get_control_db)) -> AdminInviteListOut:
    rows = control_db.queries.list_invites(control_conn)
    return AdminInviteListOut(
        invites=[
            AdminInviteListItemOut(
                id=row["id"],
                expires_at=row["expires_at"],
                used_at=row["used_at"],
                used_by_username=row["used_by_username"],
            )
            for row in rows
        ]
    )