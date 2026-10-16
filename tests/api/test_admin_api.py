"""/api/admin/* - account and invite management, always as the one admin."""

import json
import time

import pytest
from sqlalchemy import text

from tests.conftest import BOOTSTRAP_TOKEN
from tests.helpers import auth, login, make_admin, make_invite, make_user, register
from utils.worker_paths import backfill_status_path, heartbeat_path


@pytest.fixture
def admin(client):
    return make_admin(client)


def db_exists(name: str) -> bool:
    from tests.conftest import _server_engine

    eng = _server_engine()
    with eng.connect() as conn:
        found = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first() is not None
    eng.dispose()
    return found


class TestInvites:
    def test_create_list_and_the_token_is_shown_only_once(self, client, admin):
        token = make_invite(client, admin)
        listing = client.get("/api/admin/invites", headers=auth(admin))
        assert listing.status_code == 200 and len(listing.json()["invites"]) == 1
        assert token not in listing.text

    @pytest.mark.parametrize("hours", [0, -1, 721, 10_000])
    def test_lifetime_is_bounded(self, client, admin, hours):
        assert client.post("/api/admin/invites", headers=auth(admin), json={"expires_hours": hours}).status_code == 422

    def test_default_lifetime_is_24_hours(self, client, admin):
        body = client.post("/api/admin/invites", headers=auth(admin), json={}).json()
        from datetime import datetime, timezone

        delta = datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc)
        assert 23 * 3600 < delta.total_seconds() <= 24 * 3600

    def test_an_unused_invite_can_be_revoked_and_then_no_longer_registers(self, client, admin):
        token = make_invite(client, admin)
        invite_id = client.get("/api/admin/invites", headers=auth(admin)).json()["invites"][0]["id"]
        assert client.delete(f"/api/admin/invites/{invite_id}", headers=auth(admin)).status_code == 200
        assert register(client, token, "alice").status_code == 400

    def test_a_used_invite_cannot_be_deleted(self, client, admin):
        register(client, make_invite(client, admin), "alice")
        invite_id = client.get("/api/admin/invites", headers=auth(admin)).json()["invites"][0]["id"]
        assert client.delete(f"/api/admin/invites/{invite_id}", headers=auth(admin)).status_code == 409

    def test_deleting_an_unknown_invite_is_404(self, client, admin):
        assert client.delete("/api/admin/invites/99999", headers=auth(admin)).status_code == 404

    def test_there_is_no_way_to_mint_a_second_admin(self, client, admin):
        token, _ = make_user(client, admin, "alice")
        assert client.get("/api/auth/me", headers=auth(token)).json()["is_admin"] is False
        assert register(client, BOOTSTRAP_TOKEN, "sneaky").status_code == 400
        users = client.get("/api/admin/users", headers=auth(admin)).json()["users"]
        assert [u["username"] for u in users if u["is_admin"]] == ["admin"]


class TestUsers:
    def test_listing(self, client, admin):
        make_user(client, admin, "alice")
        users = client.get("/api/admin/users", headers=auth(admin)).json()["users"]
        assert {u["username"] for u in users} == {"admin", "alice"}
        alice = next(u for u in users if u["username"] == "alice")
        assert (alice["is_locked"], alice["has_archive"], alice["has_telegram_session"]) == (False, False, False)

    def test_locking_blocks_login_refresh_and_is_reversible(self, client, admin):
        _, uid = make_user(client, admin, "alice")
        client.cookies.clear()
        assert client.post(f"/api/admin/users/{uid}/lock", headers=auth(admin)).json() == {"ok": True}
        assert login(client, "alice").status_code == 403
        assert client.post(f"/api/admin/users/{uid}/unlock", headers=auth(admin)).status_code == 200
        assert login(client, "alice").status_code == 200

    def test_locking_revokes_existing_refresh_sessions(self, client, admin):
        _, uid = make_user(client, admin, "alice")
        alice_cookie = client.cookies.get("televault_refresh", path="/api/auth")
        client.post(f"/api/admin/users/{uid}/lock", headers=auth(admin))
        client.cookies.set("televault_refresh", alice_cookie, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 403

    def test_the_admin_cannot_lock_themselves(self, client, admin):
        me = client.get("/api/auth/me", headers=auth(admin)).json()["id"]
        assert client.post(f"/api/admin/users/{me}/lock", headers=auth(admin)).status_code == 400

    @pytest.mark.parametrize("action", ["lock", "unlock"])
    def test_unknown_users_are_404(self, client, admin, action):
        assert client.post(f"/api/admin/users/99999/{action}", headers=auth(admin)).status_code == 404

    def test_deleting_a_user_removes_the_account_and_its_archive_database(self, client, admin):
        token, uid = make_user(client, admin, "alice")
        ref = client.post("/api/archive/provision", headers=auth(token)).json()["archive_db_ref"]
        assert db_exists(ref)
        resp = client.delete(f"/api/admin/users/{uid}", headers=auth(admin))
        assert resp.status_code == 200 and resp.json() == {"deleted": True, "username": "alice"}
        assert not db_exists(ref)
        assert "alice" not in {u["username"] for u in client.get("/api/admin/users", headers=auth(admin)).json()["users"]}

    def test_a_deleted_accounts_token_stops_working_where_the_database_is_consulted(self, client, admin):
        token, uid = make_user(client, admin, "alice")
        client.delete(f"/api/admin/users/{uid}", headers=auth(admin))
        assert client.get("/api/auth/me", headers=auth(token)).status_code == 404
        assert client.get("/api/messages", headers=auth(token)).status_code == 409
        assert client.post("/api/telethon/start", headers=auth(token), json={}).status_code == 403

    def test_the_username_can_be_registered_again_afterwards(self, client, admin):
        _, uid = make_user(client, admin, "alice")
        client.delete(f"/api/admin/users/{uid}", headers=auth(admin))
        assert register(client, make_invite(client, admin), "alice").status_code == 201

    def test_the_admin_account_cannot_be_deleted_over_http(self, client, admin):
        me = client.get("/api/auth/me", headers=auth(admin)).json()["id"]
        assert client.delete(f"/api/admin/users/{me}", headers=auth(admin)).status_code == 400

    def test_unknown_user_delete_is_404(self, client, admin):
        assert client.delete("/api/admin/users/99999", headers=auth(admin)).status_code == 404

    def test_deletion_is_refused_while_the_accounts_archiver_runs(self, client, admin):
        token, uid = make_user(client, admin, "alice")
        ref = client.post("/api/archive/provision", headers=auth(token)).json()["archive_db_ref"]
        path = heartbeat_path(uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
        try:
            resp = client.delete(f"/api/admin/users/{uid}", headers=auth(admin))
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "already_running"
            assert db_exists(ref)  # nothing was dropped
        finally:
            path.unlink(missing_ok=True)

    def test_deletion_is_refused_while_a_backfill_runs(self, client, admin):
        import os

        _, uid = make_user(client, admin, "alice")
        path = backfill_status_path(uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        try:
            resp = client.delete(f"/api/admin/users/{uid}", headers=auth(admin))
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "backfill_running"
        finally:
            path.unlink(missing_ok=True)

    def test_another_accounts_running_archiver_does_not_block_a_deletion(self, client, admin):
        _, alice = make_user(client, admin, "alice")
        _, bob = make_user(client, admin, "bob")
        path = heartbeat_path(bob)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
        try:
            assert client.delete(f"/api/admin/users/{alice}", headers=auth(admin)).status_code == 200
        finally:
            path.unlink(missing_ok=True)
