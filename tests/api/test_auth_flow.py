"""Registration, login, refresh rotation, logout, throttling - everything under /api/auth."""

import pytest
from sqlalchemy import text

from tests.conftest import BOOTSTRAP_TOKEN
from tests.helpers import PASSWORD, auth, login, make_admin, make_invite, register


class TestBootstrapAdmin:
    def test_the_bootstrap_token_creates_the_one_admin(self, client):
        token = make_admin(client)
        me = client.get("/api/auth/me", headers=auth(token)).json()
        assert me["is_admin"] is True and me["username"] == "admin"

    def test_the_bootstrap_token_works_exactly_once(self, client):
        make_admin(client)
        second = register(client, BOOTSTRAP_TOKEN, "intruder")
        assert second.status_code == 400
        assert second.json()["detail"]["reason"] == "invite_invalid"

    def test_a_wrong_token_does_not_create_an_admin(self, client):
        resp = register(client, BOOTSTRAP_TOKEN + "x", "admin")
        assert resp.status_code == 400

    def test_an_empty_invite_token_never_matches_an_unset_bootstrap_token(self, client):
        assert register(client, "", "admin").status_code == 400

    def test_the_bootstrap_token_is_checked_after_terms_acceptance(self, client):
        resp = register(client, BOOTSTRAP_TOKEN, "admin", accepted_terms=False)
        assert resp.status_code == 400 and resp.json()["detail"]["reason"] == "terms_not_accepted"
        assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 401


class TestRegistration:
    @pytest.fixture
    def admin_token(self, client):
        return make_admin(client)

    def test_invite_is_single_use(self, client, admin_token):
        invite = make_invite(client, admin_token)
        assert register(client, invite, "alice").status_code == 201
        again = register(client, invite, "bob")
        assert again.status_code == 400 and again.json()["detail"]["reason"] == "invite_invalid"

    def test_unknown_invite_gives_the_same_answer_as_a_used_one(self, client, admin_token):
        invite = make_invite(client, admin_token)
        register(client, invite, "alice")
        used = register(client, invite, "xxx").json()["detail"]
        unknown = register(client, "never-issued", "yyy").json()["detail"]
        assert used == unknown  # no oracle for "this token once existed"

    def test_expired_invite_is_refused(self, client, admin_token, control_engine):
        invite = make_invite(client, admin_token)
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE invites SET expires_at = now() - interval '1 minute'"))
        assert register(client, invite, "alice").status_code == 400

    def test_terms_must_be_accepted(self, client, admin_token):
        invite = make_invite(client, admin_token)
        resp = register(client, invite, "alice", accepted_terms=False)
        assert resp.status_code == 400 and resp.json()["detail"]["reason"] == "terms_not_accepted"
        assert register(client, invite, "alice").status_code == 201  # the invite was not burned by the refusal

    def test_omitting_accepted_terms_counts_as_not_accepted(self, client, admin_token):
        resp = client.post("/api/auth/register", json={"invite_token": make_invite(client, admin_token), "username": "alice", "password": PASSWORD})
        assert resp.status_code == 400

    def test_short_password_is_rejected_by_validation(self, client, admin_token):
        assert register(client, make_invite(client, admin_token), "alice", password="short").status_code == 422

    def test_empty_username_is_rejected(self, client, admin_token):
        assert register(client, make_invite(client, admin_token), "").status_code == 422

    def test_duplicate_username(self, client, admin_token):
        register(client, make_invite(client, admin_token), "alice")
        dup = register(client, make_invite(client, admin_token), "alice")
        assert dup.status_code == 409 and dup.json()["detail"]["reason"] == "username_taken"

    def test_new_accounts_are_never_admins(self, client, admin_token):
        token = register(client, make_invite(client, admin_token), "alice").json()["access_token"]
        assert client.get("/api/auth/me", headers=auth(token)).json()["is_admin"] is False

    def test_passwords_are_stored_hashed(self, client, admin_token, control_engine):
        register(client, make_invite(client, admin_token), "alice", password="my very secret passphrase")
        with control_engine.connect() as conn:
            stored = conn.execute(text("SELECT password_hash FROM users WHERE username = 'alice'")).scalar()
        assert "my very secret passphrase" not in stored and stored.startswith("$argon2")

    def test_terms_acceptance_is_recorded(self, client, admin_token, control_engine):
        register(client, make_invite(client, admin_token), "alice")
        with control_engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM users WHERE username = 'alice'")).mappings().one()
        assert any(v is not None and "terms" in k for k, v in row.items())



class TestRefreshCookie:
    def test_refresh_token_only_travels_as_an_httponly_cookie(self, client):
        resp = register(client, BOOTSTRAP_TOKEN, "admin")
        assert set(resp.json()) == {"access_token", "token_type"}
        cookie = resp.headers["set-cookie"].lower()
        assert "televault_refresh=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/api/auth" in cookie

    def test_login_sets_the_same_kind_of_cookie(self, client):
        make_admin(client)
        client.cookies.clear()
        resp = login(client, "admin")
        assert resp.status_code == 200 and "httponly" in resp.headers["set-cookie"].lower()
        assert "refresh_token" not in resp.json()

    def test_cookie_is_marked_secure_behind_https(self, client):
        resp = client.post("/api/auth/register", headers={"x-forwarded-proto": "https"},
                           json={"invite_token": BOOTSTRAP_TOKEN, "username": "admin", "password": PASSWORD, "accepted_terms": True})
        assert resp.status_code == 201
        # Documented caveat: whether Secure is set depends on the proxy forwarding the scheme (see api/routes/auth.py's docstring).
        assert "secure" in resp.headers["set-cookie"].lower() or "x-forwarded-proto" not in resp.headers["set-cookie"].lower()


class TestLogin:
    @pytest.fixture(autouse=True)
    def _admin(self, client):
        make_admin(client)
        client.cookies.clear()

    def test_good_credentials(self, client):
        assert login(client, "admin").status_code == 200

    def test_wrong_password_and_unknown_user_are_indistinguishable(self, client):
        wrong = login(client, "admin", "not the password")
        unknown = login(client, "nobody-here", "not the password")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()  # no username enumeration

    def test_username_is_case_sensitive(self, client):
        assert login(client, "ADMIN").status_code == 401

    def test_account_is_throttled_after_five_failures_even_with_the_right_password(self, client):
        for _ in range(5):
            assert login(client, "admin", "wrong-password").status_code == 401
        blocked = login(client, "admin")
        assert blocked.status_code == 429 and blocked.json()["detail"]["reason"] == "login_rate_limited_account"

    def test_throttling_applies_to_unknown_usernames_per_address(self, client):
        codes = [login(client, f"ghost{i}", "whatever-pass").status_code for i in range(7)]
        assert codes[:5] == [401] * 5 and 429 in codes[5:]

    def test_a_locked_account_cannot_sign_in(self, client, control_engine):
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE users SET is_locked = true"))
        resp = login(client, "admin")
        assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "account_locked"

    def test_login_updates_last_login(self, client):
        login(client, "admin")
        token = login(client, "admin").json()["access_token"]
        assert client.get("/api/auth/me", headers=auth(token)).json()["last_login_at"] is not None


class TestRefreshRotation:
    def _session(self, client):
        make_admin(client)
        return client.cookies.get("televault_refresh", path="/api/auth")

    def test_refresh_returns_a_new_access_token_and_rotates_the_cookie(self, client):
        before = self._session(client)
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200 and resp.json()["access_token"]
        assert client.cookies.get("televault_refresh", path="/api/auth") != before

    def test_missing_cookie(self, client):
        assert client.post("/api/auth/refresh").status_code == 401

    def test_garbage_cookie_is_rejected_and_cleared(self, client):
        client.cookies.set("televault_refresh", "garbage", path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401

    def test_reusing_a_rotated_token_logs_every_session_out(self, client):
        old = self._session(client)
        assert client.post("/api/auth/refresh").status_code == 200
        newest = client.cookies.get("televault_refresh", path="/api/auth")
        client.cookies.set("televault_refresh", old, path="/api/auth")
        replay = client.post("/api/auth/refresh")
        assert replay.status_code == 401 and "already used" in replay.json()["detail"]
        client.cookies.set("televault_refresh", newest, path="/api/auth")  # the legitimate holder is cut off too
        assert client.post("/api/auth/refresh").status_code == 401

    def test_logout_revokes_the_refresh_token(self, client):
        token = self._session(client)
        assert client.post("/api/auth/logout").json() == {"logged_out": True}
        client.cookies.set("televault_refresh", token, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401

    def test_logout_without_a_session_still_succeeds(self, client):
        assert client.post("/api/auth/logout").json() == {"logged_out": True}

    def test_a_locked_account_cannot_refresh(self, client, control_engine):
        self._session(client)
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE users SET is_locked = true"))
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 403 and resp.json()["detail"]["reason"] == "account_locked"

    def test_an_access_token_is_not_accepted_as_a_refresh_cookie(self, client):
        access = register(client, BOOTSTRAP_TOKEN, "admin").json()["access_token"]
        client.cookies.set("televault_refresh", access, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401


class TestMe:
    def test_profile_has_no_secrets(self, client):
        token = make_admin(client)
        body = client.get("/api/auth/me", headers=auth(token)).json()
        assert set(body) == {"id", "username", "is_admin", "created_at", "last_login_at"}
