"""
Every client-supplied value has an upper bound (rules in utils/validation.py).

The original motivation: a password above 4096 characters made passlib RAISE, which reached the client as an unhandled 500.
These tests pin the boundary on both sides (the maximum is accepted, one more is refused with a 4xx - never a 500) and check that LOGIN stays lenient about FORMAT,
so tightening a rule later cannot lock out an account created under an older one.
"""

import pytest
from sqlalchemy import text

from tests.helpers import PASSWORD, auth, login, make_admin, make_invite, make_user, register
from utils import validation as v


@pytest.fixture
def admin(client):
    return make_admin(client)


class TestPassword:
    def test_the_maximum_length_is_accepted_and_can_log_in(self, client, admin):
        pw = "p" * v.PASSWORD_MAX_LENGTH
        assert register(client, make_invite(client, admin), "alice", password=pw).status_code == 201
        assert login(client, "alice", pw).status_code == 200

    @pytest.mark.parametrize("length", [v.PASSWORD_MAX_LENGTH + 1, 4097, 5000, 1_000_000])
    def test_longer_is_a_client_error_never_a_server_error(self, client, admin, length):
        resp = register(client, make_invite(client, admin), "alice", password="p" * length)
        assert resp.status_code == 422

    @pytest.mark.parametrize("length", [v.PASSWORD_MAX_LENGTH + 1, 5000])
    def test_login_refuses_oversized_passwords_before_any_hashing(self, client, admin, length):
        make_user(client, admin, "alice")
        assert login(client, "alice", "p" * length).status_code == 422

    def test_the_minimum_still_applies(self, client, admin):
        assert register(client, make_invite(client, admin), "alice", password="p" * (v.PASSWORD_MIN_LENGTH - 1)).status_code == 422
        assert register(client, make_invite(client, admin), "alice", password="p" * v.PASSWORD_MIN_LENGTH).status_code == 201

    def test_a_multibyte_password_at_the_limit_works(self, client, admin):
        pw = "ж" * v.PASSWORD_MAX_LENGTH  # characters are counted, not bytes
        assert register(client, make_invite(client, admin), "alice", password=pw).status_code == 201
        assert login(client, "alice", pw).status_code == 200


class TestUsername:
    @pytest.mark.parametrize("name", ["abc", "a.b-c_d", "A" * v.USERNAME_MAX_LENGTH, "user123", "123"])
    def test_valid_names_are_accepted(self, client, admin, name):
        assert register(client, make_invite(client, admin), name).status_code == 201

    @pytest.mark.parametrize(
        "name",
        ["", "ab", "A" * (v.USERNAME_MAX_LENGTH + 1), "has space", "емоджі", "a@b", "a/b", "a;b", "<script>", "al\nice", "ali\x00ce", "alice ", " alice", "al'ice", "а" * 5],
    )
    def test_invalid_names_are_refused(self, client, admin, name):
        invite = make_invite(client, admin)
        assert register(client, invite, name).status_code == 422
        assert register(client, invite, "validname").status_code == 201  # the invite was not burned by the refusal

    def test_login_only_caps_the_length(self, client, admin):
        assert login(client, "x" * (v.USERNAME_MAX_LENGTH + 1)).status_code == 422
        # any shorter string, even one registration would refuse, is just an ordinary failed login
        assert login(client, "no such user!").status_code == 401

    def test_an_account_created_under_an_older_looser_rule_can_still_sign_in(self, client, admin, control_engine):
        """Simulates a legacy account (created by the CLI before the pattern existed) - login must not re-validate its format."""
        make_user(client, admin, "legacyuser")
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE users SET username = 'old name!' WHERE username = 'legacyuser'"))
        assert login(client, "old name!").status_code == 200


class TestOtherFields:
    def test_an_oversized_invite_token_is_refused_cheaply(self, client):
        assert register(client, "t" * (v.INVITE_TOKEN_MAX_LENGTH + 1), "alice").status_code == 422

    @pytest.mark.parametrize(
        "body",
        [
            {"api_id": "1" * (v.TELEGRAM_API_ID_MAX_LENGTH + 1), "api_hash": "a" * 32},
            {"api_id": "12345", "api_hash": "a" * (v.TELEGRAM_API_HASH_MAX_LENGTH + 1)},
            {"api_id": "", "api_hash": "a" * 32},
            {"api_id": "12345", "api_hash": ""},
        ],
    )
    def test_telegram_credentials_are_bounded(self, client, admin, body):
        token, _ = make_user(client, admin, "alice")
        assert client.post("/api/telegram/credentials", headers=auth(token), json=body).status_code == 422

    def test_phone_code_and_2fa_password_are_bounded(self, client, admin):
        token, _ = make_user(client, admin, "alice")
        h = auth(token)
        assert client.post("/api/telegram/link/send-code", headers=h, json={"phone": "+" + "1" * v.TELEGRAM_PHONE_MAX_LENGTH}).status_code == 422
        assert client.post("/api/telegram/link/confirm", headers=h, json={"code": "1" * (v.TELEGRAM_CODE_MAX_LENGTH + 1)}).status_code == 422
        assert client.post("/api/telegram/link/confirm", headers=h, json={"password": "p" * (v.TELEGRAM_2FA_PASSWORD_MAX_LENGTH + 1)}).status_code == 422

    @pytest.mark.parametrize("path", ["/api/messages", "/api/deleted", "/api/chats/1/messages"])
    def test_search_parameters_are_bounded(self, client, admin, path):
        token, _ = make_user(client, admin, "alice")
        h = auth(token)
        # The archive gate runs before query validation (an unprovisioned account would get 409 first), so give the account a real archive.
        assert client.post("/api/archive/provision", headers=h).status_code in (200, 201)
        assert client.get(path, headers=h, params={"q": "x" * (v.SEARCH_QUERY_MAX_LENGTH + 1)}).status_code == 422
        assert client.get(path, headers=h, params={"date_from": "2" * (v.DATE_PARAM_MAX_LENGTH + 1)}).status_code == 422
        assert client.get(path, headers=h, params={"q": "x" * v.SEARCH_QUERY_MAX_LENGTH}).status_code != 422

    def test_too_many_chat_filters_are_refused(self, client, admin):
        token, _ = make_user(client, admin, "alice")
        assert client.post("/api/archive/provision", headers=auth(token)).status_code in (200, 201)
        too_many = [("chat_ids", i) for i in range(v.CHAT_IDS_MAX_COUNT + 1)]
        assert client.get("/api/messages", headers=auth(token), params=too_many).status_code == 422


class TestUsernameProblem:
    """The CLI has no Pydantic layer, so it calls this function; it must agree with the API schema."""

    @pytest.mark.parametrize("name", ["abc", "a.b-c_d", "A" * 32])
    def test_ok(self, name):
        assert v.username_problem(name) is None

    @pytest.mark.parametrize("name", ["", "ab", "a" * 33, "has space", "емоджі", "a@b"])
    def test_rejected(self, name):
        assert v.username_problem(name)
