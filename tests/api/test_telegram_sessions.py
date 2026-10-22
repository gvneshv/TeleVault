"""
Telegram device management (GET/DELETE /api/telegram/sessions...) and "Unlink Telegram" ending the session inside Telegram.

Telegram is replaced by FakeClient, installed where utils/telegram_sessions.py looks it up.
The fake records every request it receives,
so the tests can say not only "the response was right" but also "Telegram was (not) asked to do X" - for example that a refused request never opened a connection at all.
"""

import json
import logging
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    HashInvalidError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest

import utils.telegram_sessions as sessions_module
from tests.helpers import auth, make_admin, make_user
from utils.crypto import encrypt_secret
from utils.worker_paths import heartbeat_path

SESSION = "alice-session-string-SECRET"
IP = "203.0.113.77"
BIG_HASH = 9223372036854775807  # int64 max: not representable exactly as a JS number, which is why ids travel as strings


def authorization(hash_, current=False, **overrides):
    values = dict(
        hash=hash_,
        current=current,
        device_model="Pixel 8",
        platform="Android",
        system_version="14",
        app_name="Telegram Android",
        app_version="10.1",
        official_app=True,
        date_created=None,
        date_active=None,
        ip=IP,
        country="Ukraine",
        region="Kyiv",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeClient:
    """Stands in for TelegramClient.
    Class-level knobs are reset by the `fake` fixture for every test."""

    instances: list["FakeClient"] = []
    devices: list = []
    connect_error: Exception | None = None
    request_error: Exception | None = None
    logout_error: Exception | None = None

    def __init__(self, session, api_id, api_hash):
        self.session_arg = session
        self.requests: list = []
        self.logged_out = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self):
        if FakeClient.connect_error:
            raise FakeClient.connect_error

    async def __call__(self, request):
        self.requests.append(request)
        if FakeClient.request_error:
            raise FakeClient.request_error
        if isinstance(request, GetAuthorizationsRequest):
            return SimpleNamespace(authorizations=list(FakeClient.devices))
        return True

    async def log_out(self):
        if FakeClient.logout_error:
            raise FakeClient.logout_error
        self.logged_out = True
        return True

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.devices = [authorization(111), authorization(BIG_HASH, current=True, device_model="TeleVault server"), authorization(333, device_model="iPhone 15")]
    FakeClient.connect_error = FakeClient.request_error = FakeClient.logout_error = None
    monkeypatch.setattr(sessions_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(sessions_module, "StringSession", lambda s: s)  # the fake session strings are not real Telethon sessions
    return FakeClient


@pytest.fixture
def account(client, control_engine, fake):
    admin = make_admin(client)
    token, user_id = make_user(client, admin, "alice")
    assert client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": "12345", "api_hash": "a" * 32}).status_code == 200
    with control_engine.begin() as conn:
        conn.execute(text("UPDATE users SET telegram_session_string = :s WHERE id = :i"), {"s": encrypt_secret(SESSION), "i": user_id})
    return SimpleNamespace(token=token, user_id=user_id, admin=admin)


def reason(resp):
    return resp.json()["detail"]["reason"]


class TestListing:
    def test_lists_the_devices_with_string_ids_and_marks_televaults_own(self, client, account):
        resp = client.get("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 200
        devices = resp.json()["devices"]
        assert [d["id"] for d in devices] == ["111", str(BIG_HASH), "333"]  # exact: an int64 survives only as a string
        assert [d["current"] for d in devices] == [False, True, False]
        assert devices[0]["device_model"] == "Pixel 8" and devices[0]["ip"] == IP

    def test_is_never_cached(self, client, account):
        assert client.get("/api/telegram/sessions", headers=auth(account.token)).headers["cache-control"] == "no-store"

    def test_acts_as_the_calling_account_only(self, client, account, fake):
        bob_token, bob_id = make_user(client, account.admin, "bob")
        # bob has no link at all: he gets the "link first" 409 and Telegram is never contacted on anyone's behalf
        resp = client.get("/api/telegram/sessions", headers=auth(bob_token))
        assert resp.status_code == 409 and reason(resp) == "telegram_not_linked"
        assert fake.instances == []
        client.get("/api/telegram/sessions", headers=auth(account.token))
        assert [c.session_arg for c in fake.instances] == [SESSION]

    def test_logs_counts_but_never_locations_or_secrets(self, client, account, caplog):
        with caplog.at_level(logging.DEBUG):
            client.get("/api/telegram/sessions", headers=auth(account.token))
        logged = caplog.text
        assert "3 Telegram device" in logged
        assert IP not in logged and "Pixel" not in logged and SESSION not in logged

    def test_the_connection_is_closed_afterwards(self, client, account, fake):
        client.get("/api/telegram/sessions", headers=auth(account.token))
        assert fake.instances[0].disconnected is True

    def test_requires_a_linked_account(self, client, fake):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")
        resp = client.get("/api/telegram/sessions", headers=auth(token))
        assert resp.status_code == 409 and reason(resp) == "telegram_not_linked"

    def test_credentials_without_a_session_are_not_linked(self, client, fake):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")
        client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": "12345", "api_hash": "a" * 32})
        resp = client.get("/api/telegram/sessions", headers=auth(token))
        assert resp.status_code == 409 and reason(resp) == "telegram_not_linked"

    def test_is_refused_while_the_archiver_runs_and_telegram_is_never_contacted(self, client, account, fake):
        path = heartbeat_path(account.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
        try:
            for method, url in (("GET", "/api/telegram/sessions"), ("DELETE", "/api/telegram/sessions/111"), ("DELETE", "/api/telegram/sessions")):
                resp = client.request(method, url, headers=auth(account.token))
                assert resp.status_code == 409 and reason(resp) == "already_running", (method, url)
            assert fake.instances == []
        finally:
            path.unlink(missing_ok=True)


class TestEnding:
    def test_ends_one_other_device(self, client, account, fake):
        resp = client.delete("/api/telegram/sessions/333", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json() == {"ended": True}
        resets = [r for r in fake.instances[-1].requests if isinstance(r, ResetAuthorizationRequest)]
        assert [r.hash for r in resets] == [333]

    def test_refuses_to_end_televaults_own_session_that_is_unlinking(self, client, account, fake):
        resp = client.delete(f"/api/telegram/sessions/{BIG_HASH}", headers=auth(account.token))
        assert resp.status_code == 400 and reason(resp) == "telegram_cannot_end_current"
        assert not [r for r in fake.instances[-1].requests if isinstance(r, ResetAuthorizationRequest)]

    def test_an_unknown_device_is_a_404_and_nothing_is_ended(self, client, account, fake):
        resp = client.delete("/api/telegram/sessions/999", headers=auth(account.token))
        assert resp.status_code == 404 and reason(resp) == "telegram_device_not_found"
        assert not [r for r in fake.instances[-1].requests if isinstance(r, ResetAuthorizationRequest)]

    @pytest.mark.parametrize("bad", ["abc", "1;2", "1" * 22, "12.5"])
    def test_the_id_must_be_digits_and_bounded_before_anything_happens(self, client, account, fake, bad):
        resp = client.delete(f"/api/telegram/sessions/{bad}", headers=auth(account.token))
        assert resp.status_code in (404, 422)
        assert fake.instances == []

    def test_ends_every_other_device_at_once(self, client, account, fake):
        resp = client.delete("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json() == {"ended": True}
        assert any(isinstance(r, ResetAuthorizationsRequest) for r in fake.instances[-1].requests)

    def test_a_telegram_refusal_because_the_session_is_too_new_is_explained(self, client, account, fake):
        fake.request_error = FreshResetAuthorisationForbiddenError(request=None)
        resp = client.delete("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 409 and reason(resp) == "telegram_session_too_new"

    def test_a_device_that_vanished_between_list_and_end_is_a_404(self, client, account, fake):
        fake.request_error = None

        original_call = FakeClient.__call__

        async def vanish(self, request):
            if isinstance(request, ResetAuthorizationRequest):
                raise HashInvalidError(request=None)
            return await original_call(self, request)

        FakeClient.__call__ = vanish  # type: ignore[method-assign]
        try:
            resp = client.delete("/api/telegram/sessions/333", headers=auth(account.token))
        finally:
            FakeClient.__call__ = original_call  # type: ignore[method-assign]
        assert resp.status_code == 404 and reason(resp) == "telegram_device_not_found"


class TestTelegramProblems:
    def test_a_session_telegram_no_longer_accepts_says_to_link_again(self, client, account, fake):
        fake.request_error = AuthKeyUnregisteredError(request=None)
        resp = client.get("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 409 and reason(resp) == "telegram_session_invalid"

    def test_an_unreachable_telegram_is_a_502_not_a_hang_or_a_500(self, client, account, fake):
        fake.connect_error = ConnectionError("no route to host")
        resp = client.get("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 502 and reason(resp) == "telegram_unreachable"

    def test_flood_wait_is_a_429(self, client, account, fake):
        fake.request_error = FloodWaitError(request=None, capture=30)
        resp = client.get("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 429 and reason(resp) == "telegram_flood_wait"

    def test_a_corrupted_saved_session_is_reported_not_a_500(self, client, account, fake, monkeypatch):
        monkeypatch.setattr(sessions_module, "StringSession", StringSession)  # the real parser, which rejects the fake string
        resp = client.get("/api/telegram/sessions", headers=auth(account.token))
        assert resp.status_code == 409 and reason(resp) == "telegram_session_unreadable"

    def test_no_error_body_leaks_the_session_or_an_address(self, client, account, fake):
        fake.request_error = AuthKeyUnregisteredError(request=None)
        body = client.get("/api/telegram/sessions", headers=auth(account.token)).text
        assert SESSION not in body and IP not in body


class TestUnlinkEndsTheSessionInTelegram:
    def stored_session(self, control_engine, user_id):
        with control_engine.connect() as conn:
            return conn.execute(text("SELECT telegram_session_string FROM users WHERE id = :i"), {"i": user_id}).scalar()

    def test_unlink_logs_out_in_telegram_and_forgets_the_session(self, client, account, fake, control_engine):
        resp = client.delete("/api/telegram/session", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json() == {"unlinked": True, "ended_in_telegram": True}
        assert fake.instances[-1].logged_out is True
        assert fake.instances[-1].session_arg == SESSION
        assert self.stored_session(control_engine, account.user_id) is None

    def test_unlink_still_works_when_telegram_cannot_be_reached_and_says_so(self, client, account, fake, control_engine):
        fake.connect_error = ConnectionError("offline")
        resp = client.delete("/api/telegram/session", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json() == {"unlinked": True, "ended_in_telegram": False}
        assert self.stored_session(control_engine, account.user_id) is None

    def test_a_session_telegram_already_ended_counts_as_ended(self, client, account, fake):
        fake.logout_error = AuthKeyUnregisteredError(request=None)
        resp = client.delete("/api/telegram/session", headers=auth(account.token))
        assert resp.json()["ended_in_telegram"] is True

    def test_an_unreadable_saved_session_unlinks_but_does_not_claim_it_was_ended(self, client, account, fake, monkeypatch, control_engine):
        monkeypatch.setattr(sessions_module, "StringSession", StringSession)
        resp = client.delete("/api/telegram/session", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json()["ended_in_telegram"] is False
        assert self.stored_session(control_engine, account.user_id) is None

    def test_undecryptable_saved_values_unlink_locally(self, client, account, fake, control_engine):
        with control_engine.begin() as conn:
            conn.execute(text("UPDATE users SET telegram_session_string = 'not-a-fernet-token' WHERE id = :i"), {"i": account.user_id})
        resp = client.delete("/api/telegram/session", headers=auth(account.token))
        assert resp.status_code == 200 and resp.json()["ended_in_telegram"] is False
        assert fake.instances == []
        assert self.stored_session(control_engine, account.user_id) is None

    def test_unlinking_an_account_with_nothing_linked_does_not_touch_telegram(self, client, fake):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")
        resp = client.delete("/api/telegram/session", headers=auth(token))
        assert resp.status_code == 200 and resp.json()["ended_in_telegram"] is True
        assert fake.instances == []

    def test_unlink_is_still_refused_while_the_archiver_runs_and_nothing_is_logged_out(self, client, account, fake, control_engine):
        path = heartbeat_path(account.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
        try:
            assert client.delete("/api/telegram/session", headers=auth(account.token)).status_code == 409
        finally:
            path.unlink(missing_ok=True)
        assert fake.instances == []
        assert self.stored_session(control_engine, account.user_id) is not None
