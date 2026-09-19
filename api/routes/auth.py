"""
POST /auth/register, /auth/login, /auth/refresh, /auth/logout, GET /auth/me.

Everything here writes to the CONTROL database (accounts/invites/refresh tokens/audit log) via api.dependencies.get_control_db() - a deliberate,
narrow exception to this API server's usual "read-only against the archive DB" rule.
See control_db/connection.py's module docstring for the full reasoning;
nothing in this file ever touches db.get_db() (the archive DB).

Token transport (read this before touching anything cookie-related below):
    The refresh token travels ONLY as an httpOnly cookie (REFRESH_COOKIE_NAME),
    never in a JSON response body and never sent by the client in a request body either - register()/login()/refresh() set it via Response.set_cookie(),
    and refresh()/logout() read it via request.cookies.get(), not from a Pydantic body model.
    This is deliberate, not the previous design: an httpOnly cookie is invisible to JavaScript entirely, so a refresh token (30 days,REFRESH_TOKEN_DAYS)
    sitting in it can't be read by an XSS payload the way a value in localStorage or a JS variable could be.
    The access token still travels in the JSON response body as before - the frontend keeps it in memory only (see web/js/lib/auth.js), never persisted;
    its short lifetime (ACCESS_TOKEN_MINUTES) makes that an acceptable residual exposure in a way a 30-day token wouldn't be.

    Cookie attributes: httponly=True (always), samesite="strict"
    (the browser never attaches it to a cross-site request at all - forms, links,
    anything - which is most of what CSRF protection would otherwise need to do by hand),
    path=REFRESH_COOKIE_PATH (scoped to only the auth endpoints, so it's never even sent alongside a /api/chats request),
    and secure=<True iff this request arrived over https> (see _cookie_is_secure() below).

    DEPLOYMENT CAVEAT, same shape as the X-Forwarded-For one already on this module:
    behind Nginx, request.url.scheme only reflects the real original scheme (https) if Nginx forwards
    X-Forwarded-Proto AND Uvicorn is run with --proxy-headers (or an equivalent ProxyHeadersMiddleware) to trust it.
    Without that, every request looks like http to this process,
    so the cookie is set without the Secure attribute even when the browser-to-Nginx hop was https -
    the cookie still works (httponly+samesite are unaffected), it just loses the "never sent over plain http" backstop.
    Confirm this on the actual VPS, same as the X-Forwarded-For setting below.

Login throttling (read this before changing MAX_FAILED_LOGIN_ATTEMPTS / LOGIN_LOCKOUT_WINDOW):
    Keyed on the ACCOUNT (user_id) when the attempted username exists,
    and only falls back to client IP for the narrow case of a username that doesn't exist at all -
    see control_db/queries.py's count_recent_login_failures_for_user() count_recent_login_failures_for_unknown_username()
    docstrings for the full reasoning.
    This matters: an earlier version of this throttle was keyed purely on IP address,
    which meant one person mistyping their password repeatedly on a shared network (office Wi-Fi, a household router, a VPN)
    could lock every OTHER account on that same network out of logging in too - they'd never touched the wrong password themselves.
    Keying on the account instead means a failed-login streak only ever affects the one account it was actually failing against.

    This is a soft, self-clearing throttle, separate from users.is_locked (an explicit admin action) -
    the two are checked independently in login() below, and neither implies the other.

    IMPORTANT DEPLOYMENT CAVEAT: _client_ip() below trusts X-Forwarded-For.
    That header is only trustworthy if Nginx (per api/server.py's module docstring, this API always sits behind it)
    is actually configured to set it, e.g. `proxy_set_header X-Forwarded-For $remote_addr;`.
    Without that, every request arrives looking like it came from Nginx's own loopback address.
    Post-fix, the blast radius of that misconfiguration is much smaller than it used to be: it only affects the unknown-username counter
    (see count_recent_login_failures_for_unknown_username()'s own docstring), never a real account's ability to log in.
    Still worth confirming on the actual VPS, not assumed here.

What this file deliberately does NOT do:
    - No admin/invite-creation endpoints (POST /admin/invites etc.) - out of scope for now, see project brief.
      Invite tokens are assumed to already exist in the invites table by the time register() runs.
    - No brute-force lockout tied to users.is_locked - that column is for a human (an admin) to set deliberately;
      the automatic throttle here is a separate, self-clearing mechanism (see above).
"""

from datetime import datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from api.dependencies import get_control_db, get_current_user
from api.schemas import AccessTokenOut, LoginIn, RegisterIn, UserOut
from control_db import queries as cq
from utils.security import (
    DecodedAccessToken,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)

REFRESH_COOKIE_NAME = "televault_refresh"
REFRESH_COOKIE_PATH = "/api/auth"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str | None:
    """
    Best-effort client IP for audit logging and login throttling.

    Prefers the first hop in X-Forwarded-For (the original client, by convention - Nginx appends rather than replaces) over request.client.host,
    since the latter is always Nginx's own address once this sits behind a reverse proxy.
    See this module's docstring for the important caveat about Nginx needing to actually set this header.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _cookie_is_secure(request: Request) -> bool:
    """Whether to set the Secure attribute on the refresh cookie - see this module's docstring for the deployment caveat this depends on."""
    return request.url.scheme == "https"


def _set_refresh_cookie(response: Response, request: Request, refresh_token: str, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds()))
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=_cookie_is_secure(request),
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


def _issue_tokens(conn: Connection, response: Response, request: Request, user_id: int, is_admin: bool) -> AccessTokenOut:
    """
    Create a fresh access/refresh pair, persist the refresh token's revocation record, set it as the httpOnly cookie,
    and return only the access token for the JSON body.
    Shared by register() and login(), the two "start a new session from scratch" endpoints.
    """
    access_token = create_access_token(user_id, is_admin)
    refresh_token, jti, expires_at = create_refresh_token(user_id)
    cq.insert_refresh_token(conn, user_id, jti, expires_at)
    _set_refresh_cookie(response, request, refresh_token, expires_at)
    return AccessTokenOut(access_token=access_token)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/register", response_model=AccessTokenOut, status_code=201, summary="Consume an invite token to create an account")
def register(body: RegisterIn, request: Request, response: Response, conn: Connection = Depends(get_control_db)) -> AccessTokenOut:
    """
    Create a new (non-admin) user from a valid, unused invite token and log them straight in.

    Auto-login on success (returning an access token here rather than requiring a separate POST /auth/login right after) -
    the person just proved they hold a legitimate invite AND chose a password in the same request;
    there's no additional factor a follow-up login would check that this request hasn't already established.
    """
    invite = cq.get_valid_invite_by_token(conn, body.invite_token)
    if invite is None:
        raise HTTPException(status_code=400, detail="That invite token is invalid, expired, or already used.")

    ip_address = _client_ip(request)
    user_agent = request.headers.get("user-agent")
    password_hash = hash_password(body.password)

    try:
        user_id = cq.register_user_via_invite(
            conn,
            invite_id=invite["id"],
            username=body.username,
            password_hash=password_hash,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except IntegrityError:
        # Postgres's unique constraint on users.username - conn.rollback() already happened inside register_user_via_invite() before it re-raised,
        # so this connection is safe to keep using.
        raise HTTPException(status_code=409, detail="That username is already taken.")

    return _issue_tokens(conn, response, request, user_id, is_admin=False)


@router.post("/login", response_model=AccessTokenOut, summary="Exchange a username/password for a fresh session")
def login(body: LoginIn, request: Request, response: Response, conn: Connection = Depends(get_control_db)) -> AccessTokenOut:
    """
    Verify credentials and issue a new session (access token in the body, refresh token as an httpOnly cookie - see this module's docstring).

    Deliberately returns the SAME 401 message ("Incorrect username or password") whether the username doesn't exist or the password is wrong -
    distinguishing the two in the response would let a caller enumerate valid usernames.

    Throttling is checked AFTER looking up the user, not before, because which counter applies depends on whether the account exists -
    see this module's docstring for why that distinction is the whole fix for the "shared network locks out other accounts" problem.
    """
    ip_address = _client_ip(request)
    user_agent = request.headers.get("user-agent")

    user = cq.get_user_by_username(conn, body.username)

    if user is None:
        if cq.count_recent_login_failures_for_unknown_username(conn, ip_address, LOGIN_LOCKOUT_WINDOW) >= MAX_FAILED_LOGIN_ATTEMPTS:
            cq.record_login_blocked(conn, "login_blocked_rate_limited_unknown_username", user_id=None, ip_address=ip_address, user_agent=user_agent)
            raise HTTPException(status_code=429, detail="Too many failed login attempts from this address. Try again later.")
        cq.record_login_failure(conn, user_id=None, ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    if cq.count_recent_login_failures_for_user(conn, user["id"], LOGIN_LOCKOUT_WINDOW) >= MAX_FAILED_LOGIN_ATTEMPTS:
        cq.record_login_blocked(conn, "login_blocked_rate_limited_account", user_id=user["id"], ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=429, detail="Too many failed login attempts for this account. Try again later.")

    if user["is_locked"]:
        cq.record_login_blocked(conn, "login_blocked_locked", user_id=user["id"], ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=403, detail="This account is locked. Contact an administrator.")

    if not verify_password(body.password, user["password_hash"]):
        cq.record_login_failure(conn, user_id=user["id"], ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    cq.record_login_success(conn, user["id"], ip_address, user_agent)
    return _issue_tokens(conn, response, request, user["id"], user["is_admin"])


@router.post("/refresh", response_model=AccessTokenOut, summary="Rotate the refresh cookie for a new access token")
def refresh(request: Request, response: Response, conn: Connection = Depends(get_control_db)) -> AccessTokenOut:
    """
    Redeem the httpOnly refresh cookie for a new access token, rotating the cookie itself in the same response.
    No request body - the refresh token comes only from the cookie (see this module's docstring), never from JSON the frontend would have to hold onto.

    Expiry itself is enforced by decode_refresh_token() reading the JWT's own `exp` claim
    (the control-DB row's expires_at was set to that exact same instant when the token was issued -
    see utils.security.create_refresh_token() - so there's nothing left for this function to re-check independently).
    """
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token is None:
        raise HTTPException(status_code=401, detail="No refresh session found. Please log in again.")

    try:
        decoded = decode_refresh_token(raw_refresh_token)
    except jwt.InvalidTokenError:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    row = cq.get_refresh_token_and_owner(conn, decoded["jti"])
    if row is None:
        # A validly-signed token whose jti was never actually issued (or the row was deleted some other way) - nothing in the DB to revoke, just reject.
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    ip_address = _client_ip(request)
    user_agent = request.headers.get("user-agent")

    if row["revoked_at"] is not None:
        # This exact jti was already revoked once before - but WHY matters,
        # and revoked_reason (control_db/schema.py) is what lets us tell an ordinary logout apart from something actually suspicious,
        # instead of treating every revoked token as equally alarming.
        _clear_refresh_cookie(response)
        if row["revoked_reason"] == "logout":
            # The legitimate owner deliberately ended this session.
            # Reject, but there's nothing to investigate and nothing else to revoke - a stale client
            # (e.g. a mobile app that hadn't heard about the logout yet) retrying this token is expected, not an attack.
            raise HTTPException(status_code=401, detail="You have been logged out. Please log in again.")
        if row["revoked_reason"] == "account_locked":
            # Already handled below on the token that triggered it,
            # but a DIFFERENT still-cached client presenting one of the other tokens that got swept up in that same lock can land here -
            # same account, same answer.
            raise HTTPException(status_code=403, detail="This account is locked. Contact an administrator.")
        # revoked_reason is 'rotated', 'reuse_detected', or NULL
        # (revoked before this column existed - treated as suspicious by default, since the real reason is unknown).
        # Per the rotation model documented on refresh_tokens in control_db/schema.py,
        # a legitimate client would have moved on to its replacement rather than presenting this one again -
        # treat the whole session as compromised, not just this token.
        cq.revoke_all_refresh_tokens_and_log(conn, row["user_id"], "reuse_detected", "refresh_reuse_detected", ip_address, user_agent)
        raise HTTPException(
            status_code=401,
            detail="This refresh token was already used. All sessions for this account have been logged out as a precaution - please log in again.",
        )

    if row["owner_is_locked"]:
        _clear_refresh_cookie(response)
        cq.revoke_all_refresh_tokens_and_log(conn, row["user_id"], "account_locked", "refresh_blocked_locked", ip_address, user_agent)
        raise HTTPException(status_code=403, detail="This account is locked. Contact an administrator.")

    new_access_token = create_access_token(row["user_id"], row["owner_is_admin"])
    new_refresh_token, new_jti, new_expires_at = create_refresh_token(row["user_id"])
    cq.rotate_refresh_token(conn, decoded["jti"], row["user_id"], new_jti, new_expires_at, ip_address, user_agent)
    _set_refresh_cookie(response, request, new_refresh_token, new_expires_at)
    return AccessTokenOut(access_token=new_access_token)


@router.post("/logout", summary="Revoke the current refresh cookie")
def logout(request: Request, response: Response, conn: Connection = Depends(get_control_db)) -> dict:
    """
    Revoke the refresh token in the httpOnly cookie (if any) and clear the cookie itself.

    Always reports success, even for an already-invalid/expired/missing cookie:
    the caller's goal("stop trusting this session") is already satisfied in that case, so there's nothing to correct.
    """
    _clear_refresh_cookie(response)

    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token is None:
        return {"logged_out": True}

    try:
        decoded = decode_refresh_token(raw_refresh_token)
    except jwt.InvalidTokenError:
        return {"logged_out": True}

    ip_address = _client_ip(request)
    user_agent = request.headers.get("user-agent")
    cq.revoke_refresh_token_for_logout(conn, decoded["jti"], decoded["user_id"], ip_address, user_agent)
    return {"logged_out": True}


@router.get("/me", response_model=UserOut, summary="Current user's profile")
def me(current: DecodedAccessToken = Depends(get_current_user), conn: Connection = Depends(get_control_db)) -> UserOut:
    """
    Return the profile of whoever the access token belongs to.

    This DOES hit the database (unlike get_current_user() itself) -
    it's an explicit "tell me about my account" request, not a per-request authorization check,
    so the tradeoff that justifies skipping a DB lookup elsewhere (see get_current_user()'s docstring) doesn't apply here.
    """
    user = cq.get_user_by_id(conn, current["user_id"])
    if user is None:
        # The account existed when the access token was issued but has since been deleted some
        # other way (no such endpoint exists yet, but nothing rules it out at the DB level).
        raise HTTPException(status_code=404, detail="User no longer exists.")
    return UserOut(
        id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        created_at=user["created_at"],
        last_login_at=user["last_login_at"],
    )