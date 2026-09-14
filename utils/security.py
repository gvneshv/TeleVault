"""
Password hashing and JWT token utilities for the auth/multi-user feature.

Password hashing: argon2 via passlib, per the confirmed choice (OWASP ASVS-style hardening).
    Argon2 was picked over bcrypt/PBKDF2 as the current OWASP-recommended default for new applications;
    passlib wraps it so callers don't touch the underlying argon2-cffi API directly.

JWT: access + refresh token pair, per the confirmed choice
(over server-side sessions - see the handoff notes this is built from for the "mobile access + prior art" reasoning).
PyJWT was chosen here specifically for the implementation - it's a small, single-purpose library (encode/decode only) with no unrelated surface area,
which fits a project this size better than a larger auth-framework-shaped dependency.

Two different token types, two different lifetimes, deliberately:
    - Access tokens are short-lived (ACCESS_TOKEN_MINUTES) and carry the claims a request needs to authorize itself (user id, is_admin) -
      never checked against the database per request, that's the whole point of using a signed token instead of a session lookup.
    - Refresh tokens are long-lived (REFRESH_TOKEN_DAYS) and carry only a `jti` (unique token ID) -
      the actual revocation state lives in control_db.schema.refresh_tokens, not in the token itself.
      This module only creates/verifies the JWT envelope;
      looking up or rotating the corresponding refresh_tokens row is the caller's job (api/routes/auth.py, not yet written).

What this module deliberately does NOT do:
    - No database access.
      Whether a given (valid, unexpired) refresh token's jti has since been revoked is a question only the refresh_tokens table can answer -
      this module just proves "this token was issued by us and hasn't expired", nothing about whether it's still considered good.
    - No claim beyond what's needed to authorize a request.
      In particular, no username/email in the JWT payload - just the numeric user id - so a token itself gives an attacker no confirmation of usernames,
      and a username change never requires re-issuing outstanding tokens.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import jwt
from passlib.context import CryptContext

from config import settings

# schemes=["argon2"], deprecated="auto":
# passlib picks argon2 for new hashes and would transparently upgrade any hash using a scheme other than the current default if one existed -
# moot today (argon2 is the only scheme configured) but costs nothing to leave set for whenever a second scheme is ever added.
_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

ACCESS_TOKEN_MINUTES = 15
REFRESH_TOKEN_DAYS = 30

_JWT_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain_password: str) -> str:
    """Hash a plaintext password for storage in users.password_hash. One-way - see verify_password() to check it."""
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored hash. Returns False (not an exception) for a bad match."""
    return _pwd_context.verify(plain_password, password_hash)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

class DecodedAccessToken(TypedDict):
    user_id: int
    is_admin: bool


class DecodedRefreshToken(TypedDict):
    user_id: int
    jti: str


def create_access_token(user_id: int, is_admin: bool) -> str:
    """Issue a short-lived access token carrying just enough to authorize a request without a DB lookup."""
    now = datetime.now(timezone.utc)
    payload = {
        "type": "access",
        "sub": str(user_id),
        "is_admin": is_admin,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)


def create_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    """
    Issue a long-lived refresh token.

    Returns (token, jti, expires_at) rather than just the token string - the caller (the /auth/login or /auth/refresh route, not yet written)
    needs jti and expires_at anyway, to write the corresponding row into control_db.schema.refresh_tokens.
    Generating them here (rather than making the caller re-derive them by decoding the token it was just handed back)
    keeps "what a refresh token's claims look like" fully owned by this module.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=REFRESH_TOKEN_DAYS)
    payload = {
        "type": "refresh",
        "sub": str(user_id),
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)
    return token, jti, expires_at


def decode_access_token(token: str) -> DecodedAccessToken:
    """
    Verify and decode an access token.

    Raises jwt.InvalidTokenError (or a subclass, e.g. jwt.ExpiredSignatureError) on anything wrong - expired, bad signature, or the wrong `type` claim
    (a refresh token handed to this function is exactly as invalid as a forged one;
    the "type" check stops a refresh token, which is long-lived and only meant to mint new access tokens, from also working as one).
    Left uncaught here, same reasoning as utils/crypto.py's InvalidToken - the caller
    (a FastAPI dependency, not yet written) turns this into a 401, not this function.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return DecodedAccessToken(user_id=int(payload["sub"]), is_admin=bool(payload["is_admin"]))


def decode_refresh_token(token: str) -> DecodedRefreshToken:
    """
    Verify and decode a refresh token.
    Same "raises on anything wrong" contract as decode_access_token().

    NOT a revocation check - a token can decode successfully here and still be revoked
    (rotated away, or force-logged-out) in control_db.schema.refresh_tokens.
    The caller must check the jti against that table before trusting this token -
    this function only proves the JWT envelope itself is genuine and unexpired.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("not a refresh token")
    return DecodedRefreshToken(user_id=int(payload["sub"]), jti=payload["jti"])