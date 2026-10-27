"""handlers/on_message.py and handlers/on_edit.py - driven with fake Telethon events; the database layer is replaced by a recorder."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from telethon import types, utils
from telethon.tl.types import (
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    MessageActionPhoneCall,
    PeerChannel,
    PeerChat,
    PhoneCallDiscardReasonMissed,
)

from handlers import ingest_filter, on_edit, on_message

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bare(cls, **attrs):
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


class FakeClient:
    """Captures the handler a module registers, keyed by Telethon event class name."""

    def __init__(self):
        self.handlers = {}

    def on(self, event_builder):
        def decorator(fn):
            self.handlers[event_builder.__name__] = fn
            return fn

        return decorator


class FakeEvent:
    def __init__(self, *, chat_id: int | None = 123, sender_id: int | None = 55, text="hello", action=None, message_id=9, edit_date=None):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = SimpleNamespace(id=message_id, message=text, text=text, action=action, date=NOW, edit_date=edit_date)

    async def get_chat(self):
        return _bare(types.User, first_name="Bob", username=None)

    async def get_sender(self):
        return _bare(types.User, username="bob", first_name="Bob", last_name="B")


def run(coro):
    return asyncio.run(coro)


class _SpyMessage:
    """Wraps a fake message and records every read of its CONTENT (`.message` / `.text`); ids, dates and the service `action` are not content."""

    def __init__(self, base, touched):
        self._base, self._touched = base, touched
        self.id, self.date, self.action, self.edit_date = base.id, base.date, base.action, base.edit_date

    @property
    def message(self):
        self._touched.append("message")
        return self._base.message

    @property
    def text(self):
        self._touched.append("text")
        return self._base.text


class SpyEvent(FakeEvent):
    """A FakeEvent that records every sign of a handler reading content or asking Telegram about the chat/sender."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.touched: list[str] = []
        self.message = _SpyMessage(self.message, self.touched)

    async def get_chat(self):
        self.touched.append("get_chat")
        return await super().get_chat()

    async def get_sender(self):
        self.touched.append("get_sender")
        return await super().get_sender()


@pytest.fixture(autouse=True)
def blocked_chats(monkeypatch):
    """
    The set of chat ids the blocklist reports as blocked - empty by default, so every pre-existing test below runs as if the feature did not exist.
    Replaces only the database half (ingest_filter._lookup); the real should_skip() and the handlers' own wiring stay under test.
    """
    blocked: set[int] = set()
    monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: chat_id in blocked)
    return blocked


@pytest.fixture
def new_message_handler():
    client = FakeClient()
    on_message.register(client)
    return client.handlers["NewMessage"]


@pytest.fixture
def edited_handler():
    client = FakeClient()
    on_edit.register(client)
    return client.handlers["MessageEdited"]


@pytest.fixture
def recorded(monkeypatch):
    calls = {"persist": [], "migration": [], "edit": []}
    monkeypatch.setattr(on_message, "_persist_message", lambda *a: calls["persist"].append(a))
    monkeypatch.setattr(on_message, "_record_migration", lambda old, new: calls["migration"].append((old, new)))
    monkeypatch.setattr(on_edit, "_persist_edit", lambda *a: calls["edit"].append(a))
    return calls


class TestNewMessageHandler:
    def test_text_message_is_persisted_with_resolved_fields(self, new_message_handler, recorded):
        run(new_message_handler(FakeEvent(text="hello world")))
        assert len(recorded["persist"]) == 1
        (chat_id, chat_name, chat_type, chat_username, sender_id, username, first, last, msg_id, text, date) = recorded["persist"][0]
        assert (chat_id, chat_name, chat_type, sender_id, msg_id, text) == (123, "Bob", "private", 55, 9, "hello world")
        assert username == "bob" and date == NOW

    def test_media_only_message_is_skipped(self, new_message_handler, recorded):
        run(new_message_handler(FakeEvent(text="")))
        assert recorded["persist"] == []

    def test_missed_call_is_archived_as_a_labelled_message(self, new_message_handler, recorded):
        action = MessageActionPhoneCall(call_id=1, reason=PhoneCallDiscardReasonMissed())
        run(new_message_handler(FakeEvent(text="", action=action)))
        assert recorded["persist"][0][9] == "[Missed call]"

    def test_event_without_chat_id_is_skipped_with_a_warning(self, new_message_handler, recorded, caplog):
        with caplog.at_level(logging.WARNING, logger="handlers.on_message"):
            run(new_message_handler(FakeEvent(chat_id=None)))
        assert recorded["persist"] == []
        assert "no chat_id" in caplog.text

    def test_missing_chat_id_is_checked_before_migration_handling(self, new_message_handler, recorded):
        """Regression: the guard used to sit AFTER the migration branches, which then received chat_id=None."""
        action = MessageActionChatMigrateTo(channel_id=555)
        run(new_message_handler(FakeEvent(chat_id=None, text="", action=action)))
        assert recorded["migration"] == []

    def test_group_upgraded_to_supergroup_is_recorded_from_the_old_chat(self, new_message_handler, recorded):
        run(new_message_handler(FakeEvent(chat_id=-777, text="", action=MessageActionChatMigrateTo(channel_id=555))))
        assert recorded["migration"] == [(-777, utils.get_peer_id(PeerChannel(555)))]
        assert recorded["persist"] == []

    def test_migration_seen_from_the_new_supergroup(self, new_message_handler, recorded):
        action = MessageActionChannelMigrateFrom(title="x", chat_id=777)
        run(new_message_handler(FakeEvent(chat_id=-1000000000555, text="", action=action)))
        assert recorded["migration"] == [(utils.get_peer_id(PeerChat(777)), -1000000000555)]

    def test_database_outage_is_logged_briefly_and_never_crashes_the_listener(self, new_message_handler, monkeypatch, caplog):
        def boom(*args):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(on_message, "_persist_message", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_message"):
            run(new_message_handler(FakeEvent()))
        assert "database unreachable" in caplog.text
        assert "Traceback" not in caplog.text

    def test_unexpected_error_is_logged_and_swallowed(self, new_message_handler, monkeypatch, caplog):
        def boom(*args):
            raise ValueError("bad row")

        monkeypatch.setattr(on_message, "_persist_message", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_message"):
            run(new_message_handler(FakeEvent()))
        assert "Failed to archive message" in caplog.text


class TestEditedHandler:
    def test_edit_is_persisted(self, edited_handler, recorded):
        run(edited_handler(FakeEvent(text="edited text", edit_date=NOW)))
        assert len(recorded["edit"]) == 1
        args = recorded["edit"][0]
        assert args[0] == 123 and args[4] == 55 and args[9] == "edited text"

    def test_event_without_chat_id_is_skipped_with_a_warning(self, edited_handler, recorded, caplog):
        with caplog.at_level(logging.WARNING, logger="handlers.on_edit"):
            run(edited_handler(FakeEvent(chat_id=None)))
        assert recorded["edit"] == []
        assert "no chat_id" in caplog.text

    def test_event_without_sender_is_skipped(self, edited_handler, recorded):
        run(edited_handler(FakeEvent(sender_id=None)))
        assert recorded["edit"] == []

    def test_non_text_edit_is_skipped(self, edited_handler, recorded):
        run(edited_handler(FakeEvent(text="")))
        assert recorded["edit"] == []

    def test_database_outage_never_crashes_the_listener(self, edited_handler, monkeypatch, caplog):
        def boom(*args):
            raise OperationalError("SELECT 1", {}, Exception("down"))

        monkeypatch.setattr(on_edit, "_persist_edit", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_edit"):
            run(edited_handler(FakeEvent()))
        assert "database unreachable" in caplog.text


class TestIngestBlocklist:
    """A blocked chat is dropped BEFORE its content is read, before Telegram is asked about it, and before anything is written."""

    def test_a_blocked_chats_message_is_not_persisted_and_nothing_is_touched(self, new_message_handler, recorded, blocked_chats):
        blocked_chats.add(123)
        event = SpyEvent(chat_id=123, text="private words")
        run(new_message_handler(event))
        assert recorded["persist"] == []
        assert event.touched == []  # no content read, no get_chat(), no get_sender()

    def test_other_chats_are_still_archived(self, new_message_handler, recorded, blocked_chats):
        blocked_chats.add(999)
        run(new_message_handler(FakeEvent(chat_id=123)))
        assert len(recorded["persist"]) == 1

    def test_the_lookup_is_made_for_the_events_own_chat_id(self, new_message_handler, edited_handler, recorded, monkeypatch):
        seen = []
        monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: seen.append(chat_id) or False)
        run(new_message_handler(FakeEvent(chat_id=123)))
        run(edited_handler(FakeEvent(chat_id=456, edit_date=NOW)))
        assert seen == [123, 456]

    def test_a_blocked_chats_edit_is_not_persisted_and_nothing_is_touched(self, edited_handler, recorded, blocked_chats):
        """The edit handler inserts a message it has never seen, so without this a blocked chat would keep leaking through edits."""
        blocked_chats.add(123)
        event = SpyEvent(chat_id=123, text="edited private words", edit_date=NOW)
        run(edited_handler(event))
        assert recorded["edit"] == []
        assert event.touched == []

    def test_other_chats_edits_are_still_recorded(self, edited_handler, recorded, blocked_chats):
        blocked_chats.add(999)
        run(edited_handler(FakeEvent(chat_id=123, edit_date=NOW)))
        assert len(recorded["edit"]) == 1

    def test_a_blocked_groups_upgrade_is_still_recorded(self, new_message_handler, recorded, blocked_chats):
        """The migration service message holds no text, and recording it is what carries the block over to the new id."""
        blocked_chats.add(-777)
        run(new_message_handler(FakeEvent(chat_id=-777, text="", action=MessageActionChatMigrateTo(channel_id=555))))
        assert recorded["migration"] == [(-777, utils.get_peer_id(PeerChannel(555)))]
        assert recorded["persist"] == []

    def test_unreadable_rules_skip_the_message_rather_than_archive_it(self, new_message_handler, recorded, monkeypatch, caplog):
        def broken(chat_id):
            raise ValueError("rules unreadable")

        monkeypatch.setattr(ingest_filter, "_lookup", broken)
        event = SpyEvent(chat_id=123)
        with caplog.at_level(logging.ERROR, logger="handlers.ingest_filter"):
            run(new_message_handler(event))
        assert recorded["persist"] == [] and event.touched == []
        assert "rather than risk archiving a blocked chat" in caplog.text

    def test_unreadable_rules_skip_the_edit_too(self, edited_handler, recorded, monkeypatch):
        def broken(chat_id):
            raise ValueError("rules unreadable")

        monkeypatch.setattr(ingest_filter, "_lookup", broken)
        run(edited_handler(FakeEvent(chat_id=123, edit_date=NOW)))
        assert recorded["edit"] == []

    def test_a_blocked_chat_is_logged_without_its_content(self, new_message_handler, recorded, blocked_chats, caplog):
        blocked_chats.add(123)
        with caplog.at_level(logging.DEBUG, logger="handlers.on_message"):
            run(new_message_handler(FakeEvent(chat_id=123, text="TOP SECRET WORDS", message_id=77)))
        assert "TOP SECRET WORDS" not in caplog.text
        assert "77" in caplog.text and "123" in caplog.text
