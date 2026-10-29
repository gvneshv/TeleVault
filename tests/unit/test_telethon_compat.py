"""utils/telethon_compat.py - the awaitable-or-None wrappers around Telethon's loosely typed calls."""

from typing import cast

import pytest
from telethon import TelegramClient
from telethon.tl.types import InputPeerUser, User

from utils.telethon_compat import disconnect_client, get_authenticated_user, run_until_disconnected


class FakeClient:
    """Mimics Telethon: inside a running loop disconnect() returns a coroutine; outside one it returns None."""

    def __init__(self, returns_coroutine: bool, me=None):
        self.returns_coroutine = returns_coroutine
        self.me = me
        self.disconnected = False
        self.ran = False

    def disconnect(self):
        async def _co():
            self.disconnected = True

        if self.returns_coroutine:
            return _co()
        self.disconnected = True
        return None

    def run_until_disconnected(self):
        async def _co():
            self.ran = True

        if self.returns_coroutine:
            return _co()
        self.ran = True
        return None

    async def get_me(self):
        return self.me


def as_client(fake: "FakeClient") -> TelegramClient:
    """The wrappers under test only call a handful of methods, which FakeClient has; the cast tells the type checker that is enough."""
    return cast(TelegramClient, fake)


@pytest.mark.parametrize("returns_coroutine", [True, False])
def test_disconnect_client_handles_both_return_shapes(returns_coroutine):
    import asyncio

    client = FakeClient(returns_coroutine)
    asyncio.run(disconnect_client(as_client(client)))
    assert client.disconnected


@pytest.mark.parametrize("returns_coroutine", [True, False])
def test_run_until_disconnected_handles_both_return_shapes(returns_coroutine):
    import asyncio

    client = FakeClient(returns_coroutine)
    asyncio.run(run_until_disconnected(as_client(client)))
    assert client.ran


def test_get_authenticated_user_returns_a_user():
    import asyncio

    user = User(id=42, first_name="Alice")
    assert asyncio.run(get_authenticated_user(as_client(FakeClient(True, me=user)))).id == 42


@pytest.mark.parametrize("bad", [InputPeerUser(user_id=1, access_hash=2), None])
def test_get_authenticated_user_rejects_anything_that_is_not_a_user(bad):
    import asyncio

    with pytest.raises(RuntimeError, match="Expected Telegram to return the logged-in User"):
        asyncio.run(get_authenticated_user(as_client(FakeClient(True, me=bad))))