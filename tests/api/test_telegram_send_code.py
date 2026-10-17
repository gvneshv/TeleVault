"""
POST /api/telegram/link/send-code with a fake Telegram client (the real one is blocked in tests, see conftest).

The route's HTTP 200 only means "Telegram accepted the request"; HOW the code is delivered (an in-app message, an SMS, a call, an e-mail step) is Telegram's decision.
The route therefore logs the delivery type so "no code arrived" can be diagnosed from the server log - and must never log the phone number.
"""

import logging
from types import SimpleNamespace

import pytest
from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeSms

import api.routes.telegram as route
from tests.helpers import auth, make_admin, make_user

PHONE = "+380991234567"


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.connected = False

    async def connect(self):
        self.connected = True

    async def send_code_request(self, phone):
        return FakeClient.reply

    async def disconnect(self):
        self.connected = False

    reply = SimpleNamespace(type=SentCodeTypeApp(length=5), next_type=None, timeout=None, phone_code_hash="hash")


@pytest.fixture
def linked_account(client, monkeypatch):
    monkeypatch.setattr(route, "TelegramClient", FakeClient)
    admin = make_admin(client)
    token, user_id = make_user(client, admin, "alice")
    saved = client.post("/api/telegram/credentials", headers=auth(token), json={"api_id": "12345", "api_hash": "a" * 32})
    assert saved.status_code == 200, saved.text
    yield token, user_id
    route._pending_links.clear()


def test_the_delivery_type_is_logged_without_the_phone_number(client, linked_account, caplog):
    token, user_id = linked_account
    FakeClient.reply = SimpleNamespace(type=SentCodeTypeApp(length=5), next_type=SentCodeTypeSms(length=5), timeout=60, phone_code_hash="hash")
    with caplog.at_level(logging.INFO, logger="api.routes.telegram"):
        resp = client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": PHONE})
    assert resp.status_code == 200 and resp.json() == {"sent": True}
    assert "delivery=SentCodeTypeApp" in caplog.text and "next_delivery=SentCodeTypeSms" in caplog.text and "timeout=60s" in caplog.text
    assert f"account {user_id}" in caplog.text
    assert PHONE not in caplog.text and PHONE.lstrip("+") not in caplog.text and "hash" not in caplog.text


def test_a_reply_without_a_delivery_type_is_still_handled(client, linked_account, caplog):
    """SentCodeSuccess / SentCodePaymentRequired have no `.type`; the route must neither crash nor log nonsense."""
    token, _ = linked_account
    FakeClient.reply = SimpleNamespace(phone_code_hash="hash")
    with caplog.at_level(logging.INFO, logger="api.routes.telegram"):
        resp = client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": PHONE})
    assert resp.status_code == 200
    assert "delivery=SimpleNamespace" in caplog.text


def test_asking_again_replaces_the_pending_handshake(client, linked_account):
    token, user_id = linked_account
    FakeClient.reply = SimpleNamespace(type=SentCodeTypeApp(length=5), next_type=None, timeout=None, phone_code_hash="first")
    client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": PHONE})
    FakeClient.reply = SimpleNamespace(type=SentCodeTypeApp(length=5), next_type=None, timeout=None, phone_code_hash="second")
    client.post("/api/telegram/link/send-code", headers=auth(token), json={"phone": PHONE})
    assert route._pending_links[user_id].phone_code_hash == "second" and len(route._pending_links) == 1