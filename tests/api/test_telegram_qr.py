"""
QR-code login: POST /api/telegram/qr/start, POST /api/telegram/qr/poll, DELETE /api/telegram/qr (and the shared 2FA step, /link/confirm).

The real Telegram client is replaced by FakeClient.
Its QR object's wait() behaves like Telethon's: it blocks until the test flips a flag (a "scan"), or raises asyncio.TimeoutError (token expiry),
SessionPasswordNeededError (2FA) or an arbitrary error.
The `client` fixture keeps ONE event loop for the whole test, so the route's background task really runs between requests - which is what the production server does.
"""

import asyncio
import base64
import logging
import time
import xml.etree.ElementTree as ET

import pytest
from telethon.errors import SessionPasswordNeededError

import api.routes.telegram as route
from tests.helpers import auth, make_admin, make_user
from utils.crypto import decrypt_secret
from utils.qr import qr_data_uri

SECRET = "SECRETTOKEN"
SESSION_STRING = "fake-session-string"


class FakeQr:
    """Stands in for telethon's QRLogin: `.url`, `.recreate()`, and a wait() that ends however the test says."""

    def __init__(self):
        self.generation = 1
        self.outcome: str | None = None  # None = keep waiting; else "scan" | "timeout" | "password" | "boom"
        self.password_ok = True

    @property
    def url(self) -> str:
        return f"tg://login?token={SECRET}{self.generation}"

    async def recreate(self):
        self.generation += 1

    async def wait(self, timeout=None):
        while self.outcome is None:
            await asyncio.sleep(0.01)
        outcome, self.outcome = self.outcome, None
        if outcome == "timeout":
            raise asyncio.TimeoutError
        if outcome == "password":
            raise SessionPasswordNeededError(request=None)
        if outcome == "boom":
            raise RuntimeError(f"network exploded near {SECRET}")
        return object()  # a "User"; the route does not look at it


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, *args, **kwargs):
        self.qr = FakeQr()
        self.disconnected = False
        self.session = type("S", (), {"save": staticmethod(lambda: SESSION_STRING)})()
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def qr_login(self):
        return self.qr

    async def sign_in(self, password=None, **kwargs):
        if password != "right":
            from telethon.errors import PasswordHashInvalidError

            raise PasswordHashInvalidError(request=None)

    async def send_code_request(self, phone):
        from types import SimpleNamespace

        return SimpleNamespace(phone_code_hash="h", type=None, next_type=None, timeout=None)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def account(client, monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(route, "TelegramClient", FakeClient)
    admin = make_admin(client)
    token, user_id = make_user(client, admin, "alice")
    assert client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": "12345", "api_hash": "a" * 32}).status_code == 200
    yield token, user_id
    route._pending_links.clear()


def start(client, token):
    return client.post("/api/telegram/qr/start", headers=auth(token))


def poll(client, token):
    return client.post("/api/telegram/qr/poll", headers=auth(token))


def wait_for(client, token, state, timeout=3.0):
    """Poll until the background task has moved the state (it runs on the app's loop, so give it a moment)."""
    deadline = time.monotonic() + timeout
    while True:
        body = poll(client, token).json()
        if body["state"] == state or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


def test_qr_data_uri_is_a_valid_svg():
    uri = qr_data_uri("tg://login?token=abc")
    prefix = "data:image/svg+xml;base64,"
    assert uri.startswith(prefix)
    assert ET.fromstring(base64.b64decode(uri[len(prefix):])).tag.endswith("svg")


def test_start_needs_saved_credentials(client, monkeypatch):
    monkeypatch.setattr(route, "TelegramClient", FakeClient)
    admin = make_admin(client)
    token, _ = make_user(client, admin, "bob")
    resp = start(client, token)
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "telegram_credentials_missing"


def test_requests_need_a_signed_in_user(client):
    assert client.post("/api/telegram/qr/start").status_code == 401
    assert client.post("/api/telegram/qr/poll").status_code == 401
    assert client.delete("/api/telegram/qr").status_code == 401


def test_start_returns_a_picture_and_never_caches_it(client, account):
    token, _ = account
    resp = start(client, token)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert resp.json()["image"].startswith("data:image/svg+xml;base64,")
    assert poll(client, token).headers["cache-control"] == "no-store"


def test_waiting_state_follows_token_rotation(client, account):
    token, _ = account
    first = start(client, token).json()["image"]
    assert poll(client, token).json() == {"state": "waiting", "image": first}
    FakeClient.instances[-1].qr.outcome = "timeout"  # Telegram's token expired -> task recreates it
    deadline = time.monotonic() + 3
    body = poll(client, token).json()
    while body.get("image") == first and time.monotonic() < deadline:
        time.sleep(0.02)
        body = poll(client, token).json()
    assert body["state"] == "waiting" and body["image"] != first


def test_scan_links_the_account_and_stores_the_session_encrypted(client, account, control_engine):
    token, user_id = account
    start(client, token)
    FakeClient.instances[-1].qr.outcome = "scan"
    assert wait_for(client, token, "linked") == {"state": "linked", "image": None}
    assert user_id not in route._pending_links and FakeClient.instances[-1].disconnected
    assert client.get("/api/telegram/status", headers=auth(token)).json()["has_session"] is True
    stored = _stored_session(control_engine, user_id)
    assert stored != SESSION_STRING and decrypt_secret(stored) == SESSION_STRING
    assert poll(client, token).json()["state"] == "none"


def _stored_session(control_engine, user_id):
    from sqlalchemy import text

    with control_engine.connect() as conn:
        return conn.execute(text("SELECT telegram_session_string FROM users WHERE id = :i"), {"i": user_id}).scalar_one()


def test_two_step_verification_uses_the_shared_password_step(client, account):
    token, user_id = account
    start(client, token)
    FakeClient.instances[-1].qr.outcome = "password"
    assert wait_for(client, token, "needs_password")["state"] == "needs_password"
    wrong = client.post("/api/telegram/link/confirm", headers=auth(token), json={"password": "nope"})
    assert wrong.status_code == 400 and user_id in route._pending_links
    ok = client.post("/api/telegram/link/confirm", headers=auth(token), json={"password": "right"})
    assert ok.status_code == 200 and ok.json()["linked"] is True
    assert client.get("/api/telegram/status", headers=auth(token)).json()["has_session"] is True


def test_confirm_before_the_scan_is_refused(client, account):
    token, _ = account
    start(client, token)
    resp = client.post("/api/telegram/link/confirm", headers=auth(token), json={"code": "12345"})
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "telegram_qr_not_scanned"


def test_poll_with_nothing_pending_says_none(client, account):
    token, _ = account
    assert poll(client, token).json() == {"state": "none", "image": None}


def test_poll_does_not_see_a_code_handshake(client, account):
    token, _ = account
    assert client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": "+380991234567"}).status_code == 200
    assert poll(client, token).json()["state"] == "none"
    assert client.delete("/api/telegram/qr", headers=auth(token)).status_code == 200
    assert len(route._pending_links) == 1  # cancelling a QR must not drop a login-code handshake


def test_an_attempt_past_its_time_limit_expires(client, account, monkeypatch):
    token, user_id = account
    start(client, token)
    # The task is parked inside wait(), so it cannot notice the deadline itself:
    # the poll must enforce it (this is what stops a stuck task from keeping an attempt alive forever).
    monkeypatch.setattr(route, "QR_LOGIN_TTL_SECONDS", 0)
    assert poll(client, token).json()["state"] == "expired"
    assert user_id not in route._pending_links


def test_cancel_stops_the_task_and_drops_the_client(client, account):
    token, user_id = account
    start(client, token)
    task = route._pending_links[user_id].task
    assert client.delete("/api/telegram/qr", headers=auth(token)).status_code == 200
    assert user_id not in route._pending_links and FakeClient.instances[-1].disconnected
    deadline = time.monotonic() + 2
    while not task.done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert task.cancelled()
    assert client.delete("/api/telegram/qr", headers=auth(token)).status_code == 200  # idempotent


def test_a_failing_wait_reports_error_and_cleans_up(client, account, caplog):
    token, user_id = account
    start(client, token)
    with caplog.at_level(logging.INFO):
        FakeClient.instances[-1].qr.outcome = "boom"
        assert wait_for(client, token, "error")["state"] == "error"
    assert user_id not in route._pending_links
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


def test_starting_again_replaces_and_cancels_the_previous_attempt(client, account):
    token, user_id = account
    start(client, token)
    old_task = route._pending_links[user_id].task
    start(client, token)
    assert len(FakeClient.instances) == 2 and FakeClient.instances[0].disconnected
    assert route._pending_links[user_id].task is not old_task
    deadline = time.monotonic() + 2
    while not old_task.done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert old_task.cancelled()


def test_send_code_replaces_a_qr_attempt(client, account):
    token, user_id = account
    start(client, token)
    qr_client = FakeClient.instances[-1]
    assert client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": "+380991234567"}).status_code == 200
    assert qr_client.disconnected and not isinstance(route._pending_links[user_id], route._PendingQrLink)


def test_the_token_is_never_logged(client, account, caplog):
    token, _ = account
    with caplog.at_level(logging.DEBUG):
        start(client, token)
        poll(client, token)
        client.delete("/api/telegram/qr", headers=auth(token))
    assert SECRET not in caplog.text and "tg://login" not in caplog.text


def test_another_account_cannot_poll_or_cancel_my_attempt(client, account):
    token, user_id = account
    other, _ = make_user(client, make_admin_token(client), "carol")
    start(client, token)
    assert poll(client, other).json()["state"] == "none"
    client.delete("/api/telegram/qr", headers=auth(other))
    assert user_id in route._pending_links


def make_admin_token(client):
    """Log the admin created by the `account` fixture back in (make_admin would try to bootstrap twice)."""
    from tests.helpers import PASSWORD

    resp = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]