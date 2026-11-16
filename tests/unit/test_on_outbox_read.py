"""handlers/on_outbox_read.py - how far the OTHER person has read the owner's messages: private chats only, and never anything that is not.

Driven with real Telethon update/event objects (the private/group/channel distinction is Telethon's, and is what is under test);
the database layer is replaced by a recorder.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from telethon import events
from telethon.tl import types

from handlers import ingest_filter, on_outbox_read


class FakeClient:
    """Captures the builder AND the handler (the builder's `inbox` flag is part of what is under test)."""

    def __init__(self):
        self.builder = None
        self.handler = None

    def on(self, builder):
        def decorator(fn):
            self.builder, self.handler = builder, fn
            return fn

        return decorator


def run(coro):
    return asyncio.run(coro)


def event_from(update):
    """What Telethon really builds from an update - not a hand-made stand-in."""
    event = events.MessageRead.build(update)
    assert event is not None
    return event


def outbox_update(peer, max_id):
    return types.UpdateReadHistoryOutbox(peer=peer, max_id=max_id, pts=1, pts_count=1)


@pytest.fixture(autouse=True)
def blocked_chats(monkeypatch):
    blocked: set[int] = set()
    monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: chat_id in blocked)
    return blocked


@pytest.fixture
def stored(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(on_outbox_read, "_persist_position", lambda chat_id, max_id: calls.append((chat_id, max_id)))
    return calls


@pytest.fixture
def client():
    c = FakeClient()
    on_outbox_read.register(c)
    return c


class TestRegistration:
    def test_only_outbox_reads_are_listened_to(self, client):
        """The owner's own reading is handlers/on_read.py's job; mixing the two would draw the unread bar from somebody else's position."""
        assert isinstance(client.builder, events.MessageRead)
        assert client.builder.inbox is False

    def test_telethons_own_filter_lets_the_other_persons_reads_through_and_stops_the_owners(self, client):
        client.builder.resolved = True  # normally set when the client resolves the chat list; irrelevant here
        assert client.builder.filter(event_from(outbox_update(types.PeerUser(5), 10)))
        inbox = types.UpdateReadHistoryInbox(peer=types.PeerUser(5), max_id=10, still_unread_count=0, pts=1, pts_count=1)
        assert not client.builder.filter(event_from(inbox))


class TestLiveEvent:
    def test_a_private_chat_read_is_recorded(self, client, stored):
        run(client.handler(event_from(outbox_update(types.PeerUser(5), 10))))
        assert stored == [(5, 10)]

    def test_a_group_is_never_recorded(self, client, stored):
        """Owner's decision: following many people's reading means flood limits and legal exposure."""
        run(client.handler(event_from(outbox_update(types.PeerChat(9), 33))))
        assert stored == []

    def test_a_supergroup_or_channel_is_never_recorded(self, client, stored):
        run(client.handler(event_from(types.UpdateReadChannelOutbox(channel_id=77, max_id=40))))
        assert stored == []

    def test_nothing_read_yet_is_not_recorded(self, client, stored):
        """Telegram's max_id 0 reaches the handler as None; there is nothing to store."""
        run(client.handler(event_from(outbox_update(types.PeerUser(5), 0))))
        assert stored == []

    @pytest.mark.parametrize("chat_id, max_id", [(None, 10), (5, None), (5, -1), (5, True), (5, "10"), (5, 1.5)])
    def test_unusable_values_are_dropped(self, client, stored, chat_id, max_id):
        event = SimpleNamespace(is_private=True, chat_id=chat_id, max_id=max_id)
        run(client.handler(event))
        assert stored == []

    def test_a_blocked_chat_is_not_recorded(self, client, stored, blocked_chats):
        blocked_chats.add(5)
        run(client.handler(event_from(outbox_update(types.PeerUser(5), 10))))
        assert stored == []

    def test_unreadable_blocklist_means_not_recorded(self, client, stored, monkeypatch):
        def boom(chat_id):
            raise RuntimeError("blocklist unreadable")

        monkeypatch.setattr(ingest_filter, "_lookup", boom)
        run(client.handler(event_from(outbox_update(types.PeerUser(5), 10))))
        assert stored == []

    def test_a_database_outage_is_logged_briefly_and_never_crashes_the_listener(self, client, monkeypatch, caplog):
        def boom(chat_id, max_id):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(on_outbox_read, "_persist_position", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_outbox_read"):
            run(client.handler(event_from(outbox_update(types.PeerUser(5), 10))))
        assert any("database unreachable" in r.getMessage() for r in caplog.records)
        assert all(r.exc_info is None for r in caplog.records)  # one readable line, no traceback

    def test_an_unexpected_error_is_logged_and_swallowed(self, client, monkeypatch, caplog):
        def boom(chat_id, max_id):
            raise ValueError("bug")

        monkeypatch.setattr(on_outbox_read, "_persist_position", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_outbox_read"):
            run(client.handler(event_from(outbox_update(types.PeerUser(5), 10))))
        assert any("Could not store the read receipt of chat 5" in r.getMessage() for r in caplog.records)
