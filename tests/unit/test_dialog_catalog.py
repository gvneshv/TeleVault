"""
dialog_catalog: the account's Telegram chat list as a small file for the backfill's chat picker.

Pins: what is stored (id, name, type - from real Telethon entities), that a bad or hand-edited file can never break the reader,
that a failed refresh keeps the previous list, that names never reach the log, and that the loop publishes shortly after start and then keeps refreshing.
"""

import asyncio
import json
import logging

import pytest
from telethon import types

import dialog_catalog as dc
from tests.helpers import T0
from utils.validation import DIALOG_CATALOG_MAX_COUNT, DIALOG_CATALOG_NAME_MAX_LENGTH
from utils.worker_paths import dialogs_path

UID = 90_001


@pytest.fixture(autouse=True)
def clean_file():
    dialogs_path(UID).unlink(missing_ok=True)
    yield
    dialogs_path(UID).unlink(missing_ok=True)


def user(uid, first="Alice"):
    return types.User(id=uid, first_name=first, access_hash=1)


def group(gid, title="Family"):
    return types.Chat(id=gid, title=title, photo=types.ChatPhotoEmpty(), participants_count=3, date=T0, version=1)


def channel(cid, title, megagroup=False):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=T0, broadcast=not megagroup, megagroup=megagroup, access_hash=1)


class Dialog:
    def __init__(self, dialog_id, name, entity):
        self.id, self.name, self.entity = dialog_id, name, entity


class FakeClient:
    def __init__(self, dialogs, fail=None):
        self.dialogs, self.fail, self.calls = dialogs, fail, 0

    async def iter_dialogs(self):
        self.calls += 1
        for index, dialog in enumerate(self.dialogs):
            if self.fail is not None and index == self.fail:
                raise RuntimeError("Telegram went away")
            yield dialog


def run(coro):
    return asyncio.run(coro)


class TestPublish:
    def test_stores_id_name_and_type_of_every_chat_in_telegrams_order(self):
        client = FakeClient([
            Dialog(7, "Alice", user(7)),
            Dialog(-5, "Family", group(5)),
            Dialog(-1001, "Dev chat", channel(1001, "Dev chat", megagroup=True)),
            Dialog(-1002, "News", channel(1002, "News")),
        ])
        assert run(dc.publish_dialogs(client, UID)) == 4
        catalog = dc.read_catalog(UID)
        assert catalog is not None and catalog["updated_at"]
        assert catalog["chats"] == [
            {"chat_id": 7, "name": "Alice", "chat_type": "private"},
            {"chat_id": -5, "name": "Family", "chat_type": "group"},
            {"chat_id": -1001, "name": "Dev chat", "chat_type": "supergroup"},
            {"chat_id": -1002, "name": "News", "chat_type": "channel"},
        ]

    def test_non_ascii_names_survive(self):
        run(dc.publish_dialogs(FakeClient([Dialog(7, "Мама 🌸", user(7))]), UID))
        catalog = dc.read_catalog(UID)
        assert catalog is not None and catalog["chats"][0]["name"] == "Мама 🌸"

    def test_an_entry_without_an_integer_id_is_skipped(self):
        client = FakeClient([Dialog(None, "x", user(1)), Dialog(True, "y", user(2)), Dialog(8, "ok", user(8))])
        assert run(dc.publish_dialogs(client, UID)) == 1

    def test_the_list_is_capped(self, monkeypatch):
        monkeypatch.setattr(dc, "DIALOG_CATALOG_MAX_COUNT", 3)
        client = FakeClient([Dialog(i, f"c{i}", user(i)) for i in range(1, 10)])
        assert run(dc.publish_dialogs(client, UID)) == 3

    def test_a_failed_refresh_keeps_the_previous_list_and_logs_no_names(self, caplog):
        run(dc.publish_dialogs(FakeClient([Dialog(7, "Secret Name", user(7))]), UID))
        before = dialogs_path(UID).read_text(encoding="utf-8")
        with caplog.at_level(logging.DEBUG):
            result = run(dc.publish_dialogs(FakeClient([Dialog(8, "Other Secret", user(8)), Dialog(9, "x", user(9))], fail=1), UID))
        assert result is None
        assert dialogs_path(UID).read_text(encoding="utf-8") == before
        assert "Secret Name" not in caplog.text and "Other Secret" not in caplog.text

    def test_a_successful_pass_logs_a_count_not_names(self, caplog):
        with caplog.at_level(logging.INFO):
            run(dc.publish_dialogs(FakeClient([Dialog(7, "Private Person", user(7))]), UID))
        assert "Private Person" not in caplog.text and "1 chats" in caplog.text


class TestRead:
    def test_no_file_means_no_catalog(self):
        assert dc.read_catalog(UID) is None

    @pytest.mark.parametrize("content", ["", "not json", "[]", '{"chats": 5}', '{"nothing": []}'])
    def test_a_damaged_file_means_no_catalog_not_an_error(self, content):
        path = dialogs_path(UID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        assert dc.read_catalog(UID) is None

    def test_bad_entries_are_dropped_and_the_rest_cleaned(self):
        path = dialogs_path(UID)
        path.parent.mkdir(parents=True, exist_ok=True)
        long_name = "x" * (DIALOG_CATALOG_NAME_MAX_LENGTH + 50)
        chats = [
            "text", None, {"chat_id": "7", "name": "string id"}, {"chat_id": True, "name": "bool id"},
            {"chat_id": 1, "name": "  padded  ", "chat_type": "private"},
            {"chat_id": 2, "name": "   ", "chat_type": "weird"},
            {"chat_id": 3, "name": long_name, "chat_type": "channel"},
            {"chat_id": 1, "name": "duplicate", "chat_type": "private"},
        ]
        path.write_text(json.dumps({"updated_at": "2026-11-24T00:00:00+00:00", "chats": chats}), encoding="utf-8")
        got = dc.read_catalog(UID)
        assert got is not None
        assert [c["chat_id"] for c in got["chats"]] == [1, 2, 3]
        assert got["chats"][0]["name"] == "padded"
        assert got["chats"][1] == {"chat_id": 2, "name": None, "chat_type": "group"}
        assert got["chats"][2]["name"] == "x" * DIALOG_CATALOG_NAME_MAX_LENGTH

    def test_a_huge_file_is_capped_on_read(self):
        path = dialogs_path(UID)
        path.parent.mkdir(parents=True, exist_ok=True)
        chats = [{"chat_id": i, "name": "c", "chat_type": "private"} for i in range(1, DIALOG_CATALOG_MAX_COUNT + 500)]
        path.write_text(json.dumps({"chats": chats}), encoding="utf-8")
        got = dc.read_catalog(UID)
        assert got is not None and len(got["chats"]) == DIALOG_CATALOG_MAX_COUNT

    def test_accounts_do_not_share_a_file(self):
        run(dc.publish_dialogs(FakeClient([Dialog(7, "A", user(7))]), UID))
        assert dc.read_catalog(UID + 1) is None


class TestLoop:
    def test_it_publishes_after_the_first_delay_and_then_keeps_refreshing(self, monkeypatch):
        monkeypatch.setattr(dc, "FIRST_PASS_DELAY_SECONDS", 0.01)
        monkeypatch.setattr(dc, "REFRESH_SECONDS", 0.05)
        client = FakeClient([Dialog(7, "Alice", user(7))])

        async def scenario():
            task = asyncio.create_task(dc.dialog_catalog_loop(client, UID))
            await asyncio.sleep(0.005)
            assert client.calls == 0  # not before the delay: it must not hit Telegram together with the read-position sync
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(scenario())
        assert client.calls >= 2 and dc.read_catalog(UID) is not None

    def test_defaults_are_a_minute_and_hours(self):
        assert dc.FIRST_PASS_DELAY_SECONDS >= 30 and dc.REFRESH_SECONDS >= 3600
