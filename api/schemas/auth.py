"""
Request/response models for api/routes/auth.py.

Every client-supplied string is bounded (limits live in utils/validation.py - read its docstring for the reasoning).
This replaces an earlier decision to leave passwords unbounded: passlib refuses anything over 4096 characters by RAISING,
which reached the client as an unhandled 500, and even below that limit a huge password makes the server do argon2 work for an unauthenticated caller.

Create vs. log in: the FORMAT rules (username pattern, minimum password length) are enforced at registration only.
Login merely caps lengths, so tightening a rule later can never lock out an account created under the old one.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from utils.validation import (
    INVITE_TOKEN_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
)


class RegisterIn(BaseModel):
    """Body for POST /auth/register. invite_token is the single-use token an admin already issued out-of-band
    (invite creation itself is out of scope for now - see project brief)."""

    invite_token: str = Field(..., max_length=INVITE_TOKEN_MAX_LENGTH)
    username: str = Field(
        ...,
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
        description="3-32 characters: letters, digits, '.', '_' and '-'.",
    )
    password: str = Field(
        ...,
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description="8-128 characters. No other complexity rules imposed.",
    )
    accepted_terms: bool = Field(
        False,
        description=(
            "Must be true: the person ticked 'I agree to the Terms of Service and acknowledge the Privacy Policy'. "
            "Defaults to false (not omitted-means-true), so a client that forgets the field is refused rather than silently treated as having consented."
        ),
    )


class LoginIn(BaseModel):
    """Body for POST /auth/login."""

    # Length caps only - no pattern or minimum, so accounts created under any earlier, looser rule can still sign in.
    username: str = Field(..., max_length=USERNAME_MAX_LENGTH)
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class AccessTokenOut(BaseModel):
    """
    Returned by /auth/register, /auth/login, and /auth/refresh.

    Only the access token - the refresh token travels exclusively as an httpOnly cookie now (see api/routes/auth.py's module docstring for why),
    never in a JSON body a client could read with JavaScript and stash somewhere persistent.

    token_type is always "bearer" (RFC 6750) -
    included so the client can build the `Authorization: Bearer <access_token>` header without hard-coding the scheme name itself.
    """

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """Returned by GET /auth/me. Deliberately excludes password_hash and the telegram_* credential columns - this is a profile view, not a raw row dump."""

    id: int
    username: str
    is_admin: bool
    created_at: datetime
    last_login_at: datetime | None