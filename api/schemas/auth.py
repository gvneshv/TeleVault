"""
Request/response models for api/routes/auth.py.

Password fields (RegisterIn.password, LoginIn.password) are deliberately unbounded plain str, not given a max_length here:
passlib/argon2 hashes the raw bytes regardless of length, and rejecting "too long" passwords is a constraint some sites add for their own reasons,
not one this app needs - "no error handling for impossible scenarios" guidance.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    """Body for POST /auth/register. invite_token is the single-use token an admin already issued out-of-band
    (invite creation itself is out of scope for now - see project brief)."""

    invite_token: str
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, description="Minimum 8 characters. No other complexity rules imposed.")


class LoginIn(BaseModel):
    """Body for POST /auth/login."""

    username: str
    password: str


class RefreshIn(BaseModel):
    """Body for POST /auth/refresh."""

    refresh_token: str


class LogoutIn(BaseModel):
    """Body for POST /auth/logout."""

    refresh_token: str


class TokenPair(BaseModel):
    """
    Returned by /auth/register, /auth/login, and /auth/refresh.

    token_type is always "bearer" (RFC 6750) -
    included so the client can build the `Authorization: Bearer <access_token>` header without hard-coding the scheme name itself.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """Returned by GET /auth/me. Deliberately excludes password_hash and the telegram_* credential columns - this is a profile view, not a raw row dump."""

    id: int
    username: str
    is_admin: bool
    created_at: datetime
    last_login_at: datetime | None