"""
The access-control surface, enumerated from the app's own OpenAPI schema so a NEW endpoint is covered automatically:
every endpoint except the four sign-in ones must reject callers without a valid access token, and the admin ones must reject non-admins.
"""

import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from config import settings
from tests.helpers import auth, make_admin, make_user

PUBLIC = {("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("POST", "/api/auth/refresh"), ("POST", "/api/auth/register")}


def _operations():
    from api.server import app

    ops = []
    for path, methods in app.openapi()["paths"].items():
        for method, op in methods.items():
            ops.append((method.upper(), path, "security" in op))
    return sorted(ops)


OPERATIONS = _operations()
PROTECTED = [(m, p) for m, p, secured in OPERATIONS if (m, p) not in PUBLIC]


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _call(client, method, path, headers=None):
    kwargs = {"headers": headers or {}}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, _url(path), **kwargs)


def _token(claims: dict, secret: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {"type": "access", "sub": "1", "is_admin": True, "iat": now, "exp": now + timedelta(minutes=5), **claims}
    return jwt.encode(payload, secret or settings.jwt_secret, algorithm="HS256")


def test_the_public_surface_is_exactly_the_four_sign_in_endpoints():
    """A new unauthenticated endpoint must be a deliberate decision: adding one makes this test fail until PUBLIC is edited."""
    unsecured = {(m, p) for m, p, secured in OPERATIONS if not secured}
    assert unsecured == PUBLIC


def test_the_enumeration_actually_found_the_api():
    assert len(OPERATIONS) >= 30 and ("GET", "/api/messages") in {(m, p) for m, p, _ in OPERATIONS}


@pytest.mark.parametrize("method, path", PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
class TestEveryProtectedEndpoint:
    def test_no_token_is_401(self, client, method, path):
        assert _call(client, method, path).status_code == 401

    def test_garbage_token_is_401(self, client, method, path):
        assert _call(client, method, path, auth("not.a.jwt")).status_code == 401

    def test_token_signed_with_another_secret_is_401(self, client, method, path):
        forged = _token({}, secret="attacker-secret-" + "x" * 30)
        assert _call(client, method, path, auth(forged)).status_code == 401

    def test_expired_token_is_401(self, client, method, path):
        expired = _token({"exp": datetime.now(timezone.utc) - timedelta(seconds=1)})
        assert _call(client, method, path, auth(expired)).status_code == 401

    def test_refresh_token_is_not_an_access_token(self, client, method, path):
        refresh = _token({"type": "refresh", "jti": "x"})
        assert _call(client, method, path, auth(refresh)).status_code == 401

    def test_wrong_scheme_is_401(self, client, method, path):
        assert _call(client, method, path, {"Authorization": f"Basic {_token({})}"}).status_code == 401


ADMIN_ONLY = [(m, p) for m, p in PROTECTED if p.startswith("/api/admin")]


def test_admin_endpoints_were_found():
    assert len(ADMIN_ONLY) == 7


@pytest.mark.parametrize("method, path", ADMIN_ONLY, ids=[f"{m} {p}" for m, p in ADMIN_ONLY])
def test_a_non_admin_gets_403_on_every_admin_endpoint(client, method, path):
    admin = make_admin(client)
    user_token, _ = make_user(client, admin, "alice")
    resp = _call(client, method, path, auth(user_token))
    assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "not_admin"


def test_a_forged_is_admin_claim_does_not_help(client):
    admin = make_admin(client)
    _, user_id = make_user(client, admin, "alice")
    forged = _token({"sub": str(user_id), "is_admin": True}, secret="guess-" + "x" * 40)
    assert client.get("/api/admin/users", headers=auth(forged)).status_code == 401


ARCHIVE_ENDPOINTS = [(m, p) for m, p in PROTECTED if p.startswith(("/api/chats", "/api/messages", "/api/deleted", "/api/stats", "/api/backfill/history"))]


@pytest.mark.parametrize("method, path", ARCHIVE_ENDPOINTS, ids=[f"{m} {p}" for m, p in ARCHIVE_ENDPOINTS])
def test_an_account_without_an_archive_gets_409_not_someone_elses_data(client, method, path):
    admin = make_admin(client)
    user_token, _ = make_user(client, admin, "alice")
    resp = _call(client, method, path, auth(user_token))
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"


@pytest.mark.parametrize("path", ["/api/chats", "/api/telethon/start", "/api/backfill/start"])
def test_the_no_archive_message_is_about_the_database_not_about_linking_telegram(client, path, control_engine):
    """Telegram can be fully linked while the archive database does not exist yet (they are separate setup steps).

    The 409 used to say "Finish linking your Telegram account", which sent people back to a step they had already completed.
    The worker routes only reach the archive check once Telegram IS linked, so those two cases link the account first.
    """
    from sqlalchemy import text

    admin = make_admin(client)
    user_token, user_id = make_user(client, admin, "alice")
    if path != "/api/chats":
        with control_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET telegram_api_id = 'x', telegram_api_hash = 'x', telegram_session_string = 'x' WHERE id = :i"
                ),
                {"i": user_id},
            )
    resp = client.post(path, headers=auth(user_token), json={}) if path != "/api/chats" else client.get(path, headers=auth(user_token))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "archive_unattached"
    assert "database" in detail["message"] and "linking" not in detail["message"].lower()


@pytest.mark.parametrize("path", ["/api/telethon/start", "/api/backfill/start"])
def test_workers_cannot_be_started_before_telegram_is_linked(client, path):
    admin = make_admin(client)
    user_token, _ = make_user(client, admin, "alice")
    resp = client.post(path, headers=auth(user_token), json={})
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "telegram_not_linked"


@pytest.mark.parametrize("path", ["/api/telethon/start", "/api/backfill/start"])
def test_a_locked_account_cannot_start_workers_even_with_a_valid_token(client, path, control_engine):
    from sqlalchemy import text

    admin = make_admin(client)
    user_token, user_id = make_user(client, admin, "alice")
    with control_engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_locked = true WHERE id = :i"), {"i": user_id})
    resp = client.post(path, headers=auth(user_token), json={})
    assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "account_locked"


def test_unknown_api_paths_are_json_404_not_the_html_page(client):
    resp = client.get("/api/definitely-not-a-route")
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("application/json")


def test_api_docs_are_off_by_default(client):
    for path in ("/api/docs", "/api/redoc", "/api/openapi.json"):
        assert client.get(path).status_code == 404


class TestHealthReportsStateInTheBody:
    """/api/health is the one archive-facing route that answers 200 for every authenticated caller (see api/routes/health.py)."""

    def test_unattached_account(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "alice")
        body = client.get("/api/health", headers=auth(token)).json()
        assert (body["archive_status"], body["db_readable"], body["status"], body["db_message_count"]) == ("unattached", False, "degraded", None)

    def test_a_reference_to_a_database_that_was_never_created_is_misconfigured_not_unavailable(self, client, control_engine):
        from sqlalchemy import text

        admin = make_admin(client)
        token, user_id = make_user(client, admin, "alice")
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE users SET archive_db_ref = 'televault_test_does_not_exist' WHERE id = :i"), {"i": user_id})
        assert client.get("/api/health", headers=auth(token)).json()["archive_status"] == "misconfigured"
        resp = client.get("/api/messages", headers=auth(token))
        assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_misconfigured"

    def test_a_provisioned_archive_is_ok_and_counts_its_own_messages(self, client):
        from tests.helpers import seed_archive_by_name

        admin = make_admin(client)
        token, _ = make_user(client, admin, "alice")
        name = client.post("/api/archive/provision", headers=auth(token)).json()["archive_db_ref"]
        seed_archive_by_name(name, chat_id=1, name="c", messages=[(1, None, "x"), (2, None, "y")])
        body = client.get("/api/health", headers=auth(token)).json()
        assert (body["archive_status"], body["db_readable"], body["db_message_count"]) == ("ok", True, 2)
