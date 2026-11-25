"""
backfill.py and the ingest blocklist, end to end: the REAL run() against a real, migrated archive database,
with only Telegram faked (the autouse guard in conftest.py forbids a real connection anyway).
"""

import asyncio
import json
import logging
import signal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from telethon import types
from telethon.errors import FloodWaitError

import backfill
import db
import db.queries as q
from tests.helpers import T0
from utils.account import AccountContext


def _channel(channel_id: int, title: str):
    """A real broadcast channel entity - backfill probes channels and supergroups (and only those) for a group->supergroup migration."""
    return types.Channel(id=channel_id, title=title, photo=types.ChatPhotoEmpty(), date=T0, broadcast=True, access_hash=1)


def _user(user_id: int, first_name: str):
    """A real Telethon User (a private chat) - utils.get_peer_id() needs a proper entity, and a private chat's peer id is simply the user id."""
    return types.User(id=user_id, first_name=first_name, access_hash=1)


class FakeTelegram:
    """Just enough of TelegramClient for backfill.run(): a dialog list, per-chat history, and a record of which chats history was requested for."""

    def __init__(self, history: dict[int, list], channels: frozenset[int] = frozenset()):
        self.users = {uid: _channel(uid, f"chat{uid}") if uid in channels else _user(uid, f"chat{uid}") for uid in history}
        self.history = history
        self.fetched: list[int] = []
        # What backfill asked iter_messages for, per call: {"chat": id, "min_id": ..., "wait_time": ...}.
        self.iter_calls: list[dict] = []
        # Flood waits to inject (seconds, consumed one per request): per chat for its history, and for the chat list / the migration probe.
        self.flood_on_history: dict[int, list[int]] = {}
        self.flood_on_dialogs: list[int] = []
        self.flood_on_probe: list[int] = []
        # Called with the chat id once all of its messages were handed out - lets a test act "between two chats" (cancel, say).
        self.on_chat_done = None
        # Called with (chat id, message id) just BEFORE each message is handed out - lets a test act in the middle of a chat (cancel, say).
        self.before_message = None
        self.flood_on_entity: list[int] = []
        # Chats the account can reach but that are NOT in its chat list (one it has left, say): get_entity() still resolves them.
        self.not_in_dialogs: set[int] = set()
        self.entity_lookups: list = []

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    def disconnect(self):
        return None

    async def __call__(self, request):
        """The migration probe (GetFullChannelRequest) - only ever sent for channels and supergroups."""
        if self.flood_on_probe:
            raise FloodWaitError(request=None, capture=self.flood_on_probe.pop(0))
        return SimpleNamespace(full_chat=SimpleNamespace(migrated_from_chat_id=None))

    async def iter_dialogs(self):
        if self.flood_on_dialogs:
            raise FloodWaitError(request=None, capture=self.flood_on_dialogs.pop(0))
        for user in self.users.values():
            if user.id not in self.not_in_dialogs:
                yield SimpleNamespace(id=user.id, entity=user)

    async def get_entity(self, target):
        self.entity_lookups.append(target)
        if self.flood_on_entity:
            raise FloodWaitError(request=None, capture=self.flood_on_entity.pop(0))
        return self.users[int(target)]

    async def iter_messages(self, chat, reverse=True, limit=None, min_id=0, wait_time=None):
        self.fetched.append(chat.id)  # the moment history is requested - for a blocked chat this must never happen
        self.iter_calls.append({"chat": chat.id, "min_id": min_id, "wait_time": wait_time})
        if self.flood_on_history.get(chat.id):
            raise FloodWaitError(request=None, capture=self.flood_on_history[chat.id].pop(0))
        for i, item in enumerate(self.history[chat.id], start=1):
            # An item is the text, or (text, media) for a message that carries a file (its text is then a caption),
            # or (text, media, ttl_period) for a message with an auto-delete timer.
            text, media, ttl_period = (item + (None, None))[:3] if isinstance(item, tuple) else (item, None, None)

            async def get_sender(_chat=chat):
                return _user(7, "Sender")

            if self.before_message:
                self.before_message(chat.id, i)

            yield SimpleNamespace(
                id=i, message=text, media=media, action=None, sender_id=7, date=T0, ttl_period=ttl_period, get_sender=get_sender
            )
        if self.on_chat_done:
            self.on_chat_done(chat.id)


@pytest.fixture
def telegram(archive, monkeypatch, tmp_path):
    """Wire backfill.run() to the test archive and a fake Telegram. Returns a function that runs a whole backfill and gives back (fake, status)."""
    account = AccountContext(
        user_id=1, username="tester", api_id=1, api_hash="x", session_string="s", archive_db_name=archive.name
    )
    status_file = tmp_path / "backfill_status.json"
    monkeypatch.setattr(backfill, "load_account_context", lambda user_id: account)
    monkeypatch.setattr(backfill, "StringSession", lambda s: s)
    monkeypatch.setattr(backfill, "_status_file", status_file)
    monkeypatch.setattr(backfill, "_cancelled", False)

    async def me(client):
        return SimpleNamespace(id=1, first_name="Me")

    monkeypatch.setattr(backfill, "get_authenticated_user", me)

    previous_sigterm = signal.getsignal(signal.SIGTERM)  # run() installs its own handler; do not leak it into other tests

    # Every deliberate pause (between chats, through a flood wait) is recorded instead of slept, so the tests stay fast and can assert on the pauses.
    sleeps: list[float] = []

    # The status file as it was at each pause: shows what the web UI would have seen while the run was waiting.
    snapshots: list[dict] = []

    async def record_sleep(seconds):
        sleeps.append(seconds)
        snapshots.append(json.loads(status_file.read_text()))

    monkeypatch.setattr(backfill, "_sleep_unless_cancelled", record_sleep)

    def go(history: dict[int, list], chat_selector: str | None = None, *, force_full: bool = False, setup=None, channels: frozenset[int] = frozenset(), mode: str = "all"):
        """Run a whole backfill. `setup(fake)` may inject flood waits or hooks before it starts."""
        fake = FakeTelegram(history, channels)
        if setup:
            setup(fake)
        monkeypatch.setattr(backfill, "TelegramClient", lambda *args, **kwargs: fake)
        asyncio.run(backfill.run(1, chat_selector, None, force_full, mode))
        return fake, json.loads(status_file.read_text())

    go.sleeps = sleeps  # type: ignore[attr-defined]
    go.snapshots = snapshots  # type: ignore[attr-defined]
    yield go
    signal.signal(signal.SIGTERM, previous_sigterm)
    db.close_db()


def stored(conn):
    conn.rollback()  # see the rows backfill.run() committed from its own connection
    return {
        row[0]: row[1]
        for row in conn.execute(sa.text("SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id"))
    }


def chat_rows(conn):
    conn.rollback()
    return {r[0] for r in conn.execute(sa.text("SELECT chat_id FROM chats"))}


def test_without_a_blocklist_every_chat_is_archived(archive, telegram):
    fake, status = telegram({11: ["a", "b"], 12: ["c"]})
    assert stored(archive.conn) == {11: 2, 12: 1}
    assert status["state"] == "completed"


def test_a_blocked_chat_is_not_fetched_and_nothing_of_it_is_stored(archive, telegram):
    q.block_chat(archive.conn, 11, "chat11")
    fake, status = telegram({11: ["secret one", "secret two"], 12: ["fine"]})
    assert fake.fetched == [12]  # history of the blocked chat was never even requested
    assert stored(archive.conn) == {12: 1}
    assert 11 not in chat_rows(archive.conn)  # not even a chats row: nothing about it is written
    assert status["state"] == "completed" and status["chats_done"] == 2  # a skipped chat still counts as handled


def test_naming_a_blocked_chat_explicitly_does_not_override_the_block(archive, telegram, caplog):
    q.block_chat(archive.conn, 11, "chat11")
    with caplog.at_level(logging.INFO, logger="backfill"):
        fake, status = telegram({11: ["secret"]}, chat_selector="11")
    assert fake.fetched == [] and stored(archive.conn) == {}
    assert "on your ingest blocklist" in caplog.text
    assert "1 chat(s) skipped because they are blocked" in caplog.text


def test_unblocking_makes_the_chat_archivable_again(archive, telegram):
    q.block_chat(archive.conn, 11, "chat11")
    q.unblock_chat(archive.conn, 11)
    telegram({11: ["back again"]})
    assert stored(archive.conn) == {11: 1}


def test_unreadable_rules_fail_that_chat_instead_of_archiving_it(archive, telegram, monkeypatch, caplog):
    def broken(conn, chat_id):
        raise RuntimeError("rules unreadable")

    monkeypatch.setattr(db.queries, "is_chat_blocked", broken)
    with caplog.at_level(logging.ERROR, logger="backfill"):
        fake, status = telegram({11: ["maybe blocked"]})
    assert fake.fetched == [] and stored(archive.conn) == {}
    assert "Failed to backfill" in caplog.text
    assert status["state"] == "error"
