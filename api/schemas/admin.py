"""
Request/response models for api/routes/admin.py.

AdminUserOut is deliberately a DIFFERENT shape from auth.py's UserOut, not a superset built by adding fields to it:
UserOut is "your own profile, as you'd see it" (GET /auth/me);
AdminUserOut is "one row in an admin's user list" and needs a few things a self-view never would (is_locked, whether Telegram/an archive are set up at all)
while still never including password_hash or a live Telegram credential value - same exclusion,
different reason (an admin managing accounts has no more business reading someone's Telegram session string than that person's own profile view does).
"""

from datetime import datetime

from pydantic import BaseModel, Field


class AdminUserOut(BaseModel):
    """One row of GET /admin/users. has_telegram_session / has_archive are booleans, never the underlying values - see this module's own docstring for why."""

    id: int
    username: str
    is_admin: bool
    is_locked: bool
    created_at: datetime
    last_login_at: datetime | None
    has_telegram_session: bool
    has_archive: bool


class AdminUserListOut(BaseModel):
    users: list[AdminUserOut]


class AdminActionOut(BaseModel):
    """Returned by POST /admin/users/{id}/lock and /unlock - deliberately minimal (the action either happened or the route already raised),
    same shape as api/schemas/telegram.py's TelegramUnlinkOut for the same reason."""

    ok: bool = True


class AdminUserDeleteOut(BaseModel):
    """Returned by DELETE /admin/users/{id}.
    Echoes back which username was deleted
    (the id alone doesn't confirm THIS was the account the caller meant, and there's no row left afterward to look it up from again)."""

    deleted: bool = True
    username: str


class AdminInviteCreateIn(BaseModel):
    """Body for POST /admin/invites. expires_hours mirrors scripts/manage_admin.py's own --expires-hours default (24)."""

    expires_hours: int = Field(default=24, ge=1, le=24 * 30, description="Invite lifetime in hours (1 to 720, i.e. up to 30 days).")


class AdminInviteOut(BaseModel):
    """Returned by POST /admin/invites.
    `token` is the whole invite - shown exactly once, at creation,
    same as scripts/manage_admin.py's create-invite subcommand prints it exactly once to the terminal and never again;
    there is no GET endpoint that can recover a token already handed out (see GET /admin/invites' own docstring for what it returns instead)."""

    token: str
    expires_at: datetime


class AdminInviteListItemOut(BaseModel):
    """One row of GET /admin/invites. Deliberately omits `token` itself -
    see AdminInviteOut's own docstring for why a past invite's token can never be re-displayed, only its metadata."""

    id: int
    expires_at: datetime
    used_at: datetime | None
    used_by_username: str | None


class AdminInviteListOut(BaseModel):
    invites: list[AdminInviteListItemOut]