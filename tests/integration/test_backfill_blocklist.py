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

import backfill
import db
import db.queries as q
from tests.helpers import T0
from utils.account import AccountContext


def _user(user_id: int, first_name: str):
    """A real Telethon User (a private chat) - utils.get_peer_id() needs a proper entity, and a private chat's peer id is simply the user id."""
    return types.User(id=user_id, first_name=first_name, access_hash=1)


class FakeTelegram:
    """Just enough of TelegramClient for backfill.run(): a dialog list, per-chat history, and a record of which chats history was requested for."""

    def __init__(self, history: dict[int, list[str]]):
        self.users = {uid: _user(uid, f"chat{uid}") for uid in history}
        self.history = history
        self.fetched: list[int] = []

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    def disconnect(self):
        return None

    async def iter_dialogs(self):
        for user in self.users.values():
            yield SimpleNamespace(entity=user)

    async def get_entity(self, target):
        return self.users[int(target)]

    async def iter_messages(self, chat, reverse=True, limit=None, min_id=0):
        self.fetched.append(chat.id)  # the moment history is requested - for a blocked chat this must never happen
        for i, text in enumerate(self.history[chat.id], start=1):
            async def get_sender(_chat=chat):
                return _user(7, "Sender")

            yield SimpleNamespace(id=i, message=text, action=None, sender_id=7, date=T0, get_sender=get_sender)


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

    def go(history: dict[int, list[str]], chat_selector: str | None = None):
        fake = FakeTelegram(history)
        monkeypatch.setattr(backfill, "TelegramClient", lambda *args, **kwargs: fake)
        asyncio.run(backfill.run(1, chat_selector, None))
        return fake, json.loads(status_file.read_text())

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
