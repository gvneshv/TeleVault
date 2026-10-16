"""
Nothing secret may ever leave the server or be stored in the clear: Telegram credentials and sessions, password hashes, refresh tokens.
Checked by planting recognisable values, then searching every GET response, the audit log and the token table for them.
"""

import re

import pytest
from sqlalchemy import text

from control_db import queries as cq
from tests.helpers import PASSWORD, auth, make_admin, make_user
from utils.crypto import decrypt_secret, encrypt_secret

API_ID = "1234567"
API_HASH = "0123456789abcdef0123456789abcdef"
SESSION = "1BVtsOKwBu-SECRET-SESSION-STRING-0123456789"
NEEDLES = [API_ID, API_HASH, SESSION, "argon2", PASSWORD]


@pytest.fixture
def world(client, control_engine):
    admin = make_admin(client)
    token, user_id = make_user(client, admin, "alice")
    resp = client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": API_ID, "api_hash": API_HASH})
    assert resp.status_code == 200, resp.text
    with control_engine.connect() as conn:
        cq.set_telegram_session(conn, user_id, encrypt_secret(SESSION))
    return {"admin": admin, "token": token, "user_id": user_id, "engine": control_engine}


def _gets():
    from api.server import app

    return [re.sub(r"\{[^}]+\}", "1", p) for p, methods in app.openapi()["paths"].items() if "get" in methods]


def test_credentials_are_encrypted_at_rest(world):
    with world["engine"].connect() as conn:
        row = conn.execute(text("SELECT telegram_api_id, telegram_api_hash, telegram_session_string FROM users WHERE id = :i"), {"i": world["user_id"]}).one()
    assert all(value and not any(n in value for n in (API_ID, API_HASH, SESSION)) for value in row)
    assert [decrypt_secret(v) for v in row] == [API_ID, API_HASH, SESSION]


@pytest.mark.parametrize("who", ["token", "admin"])
@pytest.mark.parametrize("path", _gets())
def test_no_get_response_contains_a_secret(client, world, who, path):
    resp = client.get(path, headers=auth(world[who]))
    body = resp.text
    leaked = [n for n in NEEDLES if n in body]
    assert not leaked, f"GET {path} as {who} leaked {leaked}"


def test_the_linking_status_reports_booleans_only(client, world):
    body = client.get("/api/telegram/status", headers=auth(world["token"])).json()
    assert body["has_credentials"] is True and body["has_session"] is True
    assert not any(isinstance(v, str) and v for v in body.values()) or all(n not in str(body) for n in NEEDLES)


def test_the_admin_user_list_shows_that_a_session_exists_but_never_what_it_is(client, world):
    users = client.get("/api/admin/users", headers=auth(world["admin"])).json()["users"]
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["has_telegram_session"] is True
    assert set(alice) == {"id", "username", "is_admin", "is_locked", "created_at", "last_login_at", "has_telegram_session", "has_archive"}


def test_credentials_endpoint_does_not_echo_them_back(client, control_engine):
    admin = make_admin(client)
    token, _ = make_user(client, admin, "alice")
    resp = client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": API_ID, "api_hash": API_HASH})
    assert resp.json() == {"saved": True}


def test_the_audit_log_and_token_table_contain_no_secrets(client, world):
    client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password-1"})
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    client.post("/api/auth/refresh")
    with world["engine"].connect() as conn:
        dump = " ".join(str(v) for table in ("auth_audit_log", "refresh_tokens", "invites") for row in conn.execute(text(f"SELECT * FROM {table}")) for v in row)
    assert not [n for n in (API_ID, API_HASH, SESSION, PASSWORD, "wrong-password-1", "argon2") if n in dump]


def test_raw_refresh_tokens_are_never_stored_only_their_ids(client, world):
    cookie = client.cookies.get("televault_refresh", path="/api/auth")
    assert cookie
    with world["engine"].connect() as conn:
        stored = " ".join(str(v) for row in conn.execute(text("SELECT * FROM refresh_tokens")) for v in row)
    assert cookie not in stored and "eyJ" not in stored


def test_unlinking_telegram_wipes_the_stored_session(client, world):
    resp = client.delete("/api/telegram/session", headers=auth(world["token"]))
    assert resp.status_code == 200
    with world["engine"].connect() as conn:
        assert conn.execute(text("SELECT telegram_session_string FROM users WHERE id = :i"), {"i": world["user_id"]}).scalar() is None
    status = client.get("/api/telegram/status", headers=auth(world["token"])).json()
    assert status["has_session"] is False


def test_unlinking_is_refused_while_the_accounts_archiver_is_running(client, world):
    import json
    import time

    from utils.worker_paths import heartbeat_path

    path = heartbeat_path(world["user_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
    try:
        resp = client.delete("/api/telegram/session", headers=auth(world["token"]))
        assert resp.status_code == 409
        with world["engine"].connect() as conn:
            assert conn.execute(text("SELECT telegram_session_string FROM users WHERE id = :i"), {"i": world["user_id"]}).scalar() is not None
    finally:
        path.unlink(missing_ok=True)


def test_one_account_cannot_see_whether_another_has_credentials(client, world):
    admin = world["admin"]
    other_token, _ = make_user(client, admin, "bob")
    body = client.get("/api/telegram/status", headers=auth(other_token)).json()
    assert body["has_credentials"] is False and body["has_session"] is False
