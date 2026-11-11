"""handlers/on_read.py - the owner's Telegram reading position: the live event, the start-up sync and what must never be recorded.

Driven with real Telethon update/event objects where the semantics matter (a played voice note looks like a read event), and fakes elsewhere;
the database layer is replaced by a recorder.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from telethon import events
from telethon.tl import types

import db
from handlers import ingest_filter, on_read


class FakeClient:
    """Captures the builder AND the handler (the builder's `inbox` flag is part of what is under test)."""

    def __init__(self, dialogs=(), dialogs_error: Exception | None = None):
        self.builder = None
        self.handler = None
        self._dialogs = list(dialogs)
        self._dialogs_error = dialogs_error

    def on(self, builder):
        def decorator(fn):
            self.builder, self.handler = builder, fn
            return fn

        return decorator

    async def iter_dialogs(self):
        if self._dialogs_error:
            raise self._dialogs_error
        for dialog in self._dialogs:
            yield dialog


def dialog(chat_id, position):
    """The shape of Telethon's custom Dialog: `.id`, and the raw TL dialog (which carries the position) as `.dialog`."""
    return SimpleNamespace(id=chat_id, dialog=SimpleNamespace(read_inbox_max_id=position))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def blocked_chats(monkeypatch):
    blocked: set[int] = set()
    monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: chat_id in blocked)
    return blocked


@pytest.fixture
def stored(monkeypatch):
    calls = {"one": [], "many": []}
    monkeypatch.setattr(on_read, "_persist_position", lambda chat_id, max_id: calls["one"].append((chat_id, max_id)))
    monkeypatch.setattr(on_read, "_persist_positions", lambda positions: calls["many"].append(list(positions)) or len(positions))
    return calls


@pytest.fixture
def client():
    c = FakeClient()
    on_read.register(c)
    return c


def event_from(update):
    """What Telethon really builds from an update - not a hand-made stand-in."""
    event = events.MessageRead.build(update)
    assert event is not None
    return event


def inbox_update(peer, max_id):
    return types.UpdateReadHistoryInbox(peer=peer, max_id=max_id, still_unread_count=0, pts=1, pts_count=1)


class TestRegistration:
    def test_only_inbox_reads_are_listened_to(self, client):
        """Outbox reads (someone read the owner's messages) are another feature; recording them would write another person's position as the owner's."""
        assert isinstance(client.builder, events.MessageRead)
        assert client.builder.inbox is True

    def test_telethons_own_filter_lets_the_owners_reads_through_and_stops_the_others(self, client):
        client.builder.resolved = True  # normally set when the client resolves the chat list; irrelevant here
        assert client.builder.filter(event_from(inbox_update(types.PeerUser(5), 10)))
        outbox = types.UpdateReadHistoryOutbox(peer=types.PeerUser(5), max_id=10, pts=1, pts_count=1)
        assert not client.builder.filter(event_from(outbox))


class TestLiveEvent:
    def test_a_private_chat_read_is_recorded(self, client, stored):
        run(client.handler(event_from(inbox_update(types.PeerUser(5), 10))))
        assert stored["one"] == [(5, 10)]

    def test_a_group_read_is_recorded_under_the_group_id(self, client, stored):
        run(client.handler(event_from(inbox_update(types.PeerChat(9), 33))))
        assert stored["one"] == [(-9, 33)]

    def test_a_channel_read_is_recorded_under_the_marked_channel_id(self, client, stored):
        update = types.UpdateReadChannelInbox(channel_id=77, max_id=40, still_unread_count=0, pts=1)
        run(client.handler(event_from(update)))
        assert stored["one"] == [(-1000000000077, 40)]

    def test_a_played_voice_note_is_not_a_reading_position(self, client, stored):
        """Telegram reports it with the same event type, and `max_id` is the id of that one message."""
        update = types.UpdateReadMessagesContents(messages=[12], pts=1, pts_count=1)
        event = event_from(update)
        assert event.contents is True and event.max_id == 12  # the trap: it looks like a position
        run(client.handler(event))
        assert stored["one"] == []

    def test_a_played_voice_note_in_a_channel_is_not_a_reading_position_either(self, client, stored):
        update = types.UpdateChannelReadMessagesContents(channel_id=77, messages=[12])
        event = event_from(update)
        assert event.chat_id is not None  # unlike the private-chat case, nothing else would stop it
        run(client.handler(event))
        assert stored["one"] == []

    def test_nothing_read_yet_is_not_recorded(self, client, stored):
        """Telegram's max_id 0 reaches the handler as None; there is nothing to store."""
        run(client.handler(event_from(inbox_update(types.PeerUser(5), 0))))
        assert stored["one"] == []

    @pytest.mark.parametrize("chat_id, max_id", [(None, 10), (5, None), (5, -1), (5, True), (5, "10"), (5, 1.5)])
    def test_unusable_values_are_dropped(self, client, stored, chat_id, max_id):
        event = SimpleNamespace(contents=False, chat_id=chat_id, max_id=max_id)
        run(client.handler(event))
        assert stored["one"] == []

    def test_a_blocked_chat_is_not_recorded(self, client, stored, blocked_chats):
        blocked_chats.add(5)
        run(client.handler(event_from(inbox_update(types.PeerUser(5), 10))))
        assert stored["one"] == []

    def test_unreadable_blocklist_means_not_recorded(self, client, stored, monkeypatch):
        def boom(chat_id):
            raise RuntimeError("blocklist unreadable")

        monkeypatch.setattr(ingest_filter, "_lookup", boom)
        run(client.handler(event_from(inbox_update(types.PeerUser(5), 10))))
        assert stored["one"] == []

    def test_a_database_outage_is_logged_briefly_and_never_crashes_the_listener(self, client, monkeypatch, caplog):
        def boom(chat_id, max_id):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(on_read, "_persist_position", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_read"):
            run(client.handler(event_from(inbox_update(types.PeerUser(5), 10))))
        assert any("database unreachable" in r.getMessage() for r in caplog.records)
        assert all(r.exc_info is None for r in caplog.records)  # one readable line, no traceback

    def test_an_unexpected_error_is_logged_and_swallowed(self, client, monkeypatch, caplog):
        def boom(chat_id, max_id):
            raise ValueError("bug")

        monkeypatch.setattr(on_read, "_persist_position", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_read"):
            run(client.handler(event_from(inbox_update(types.PeerUser(5), 10))))
        assert any("Could not store the read position of chat 5" in r.getMessage() for r in caplog.records)


class TestStartupSync:
    def test_every_dialog_with_a_position_is_stored(self, stored):
        c = FakeClient([dialog(5, 10), dialog(-9, 0), dialog(-1000000000077, 40)])
        run(on_read.sync_read_state(c))
        assert stored["many"] == [[(5, 10), (-9, 0), (-1000000000077, 40)]]  # 0 is a real position: nothing read yet

    def test_dialogs_without_a_usable_position_are_left_out(self, stored):
        c = FakeClient([dialog(5, None), dialog(6, -3), dialog(7, 12), SimpleNamespace(id=8), SimpleNamespace(id=None, dialog=SimpleNamespace(read_inbox_max_id=4))])
        run(on_read.sync_read_state(c))
        assert stored["many"] == [[(7, 12)]]

    def test_a_failing_chat_list_is_logged_and_swallowed(self, stored, caplog):
        c = FakeClient(dialogs_error=RuntimeError("flood"))
        with caplog.at_level(logging.ERROR, logger="handlers.on_read"):
            run(on_read.sync_read_state(c))
        assert any("Could not sync the chats' read positions" in r.getMessage() for r in caplog.records)
        assert stored["many"] == []

    def test_a_database_outage_is_logged_briefly(self, monkeypatch, caplog):
        def boom(positions):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(on_read, "_persist_positions", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.on_read"):
            run(on_read.sync_read_state(FakeClient([dialog(5, 10)])))
        assert any("database unreachable" in r.getMessage() for r in caplog.records)
        assert all(r.exc_info is None for r in caplog.records)

    def test_cancellation_still_stops_it(self, monkeypatch):
        """Shutdown cancels the task; swallowing that would leave the archiver hanging on exit."""

        def cancelled(positions):
            raise asyncio.CancelledError()

        monkeypatch.setattr(on_read, "_persist_positions", cancelled)
        with pytest.raises(asyncio.CancelledError):
            run(on_read.sync_read_state(FakeClient([dialog(5, 10)])))


class TestPersistPositions:
    """The synchronous half of the sync, with the database replaced by a recorder."""

    @pytest.fixture
    def database(self, monkeypatch):
        written: list[list[tuple[int, int]]] = []

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(db, "get_connection", lambda: Conn())
        monkeypatch.setattr(db.queries, "record_read_inboxes", lambda conn, positions: written.append(list(positions)) or True)
        return written

    def test_blocked_chats_are_filtered_out_before_anything_is_written(self, database, monkeypatch):
        monkeypatch.setattr(db.queries, "is_chat_blocked", lambda conn, chat_id: chat_id == 6)
        assert on_read._persist_positions([(5, 10), (6, 20), (7, 30)]) == 2
        assert database == [[(5, 10), (7, 30)]]

    def test_an_unreadable_blocklist_abandons_the_whole_sync(self, database, monkeypatch):
        def boom(conn, chat_id):
            raise RuntimeError("blocklist unreadable")

        monkeypatch.setattr(db.queries, "is_chat_blocked", boom)
        with pytest.raises(RuntimeError):
            on_read._persist_positions([(5, 10)])
        assert database == []


class TestWiring:
    """main.py: the handler is registered with the others, and the start-up sync runs in the background and is stopped on shutdown."""

    def test_register_handlers_includes_the_read_handler(self):
        import main

        class Recorder:
            def __init__(self):
                self.builders = []

            def on(self, builder):
                self.builders.append(builder)
                return lambda fn: fn

        recorder = Recorder()
        main.register_handlers(recorder, self_id=1)  # type: ignore[arg-type]
        reads = [b for b in recorder.builders if isinstance(b, events.MessageRead)]
        assert len(reads) == 1 and reads[0].inbox is True

    def test_the_sync_is_a_background_task_that_shutdown_cancels(self):
        """Running main() needs a real Telegram login, so this pins the wiring by reading the source (as the archive-owner test does)."""
        import inspect

        import main

        source = inspect.getsource(main.main)
        start = source.index("asyncio.create_task(on_read.sync_read_state(client))")  # not awaited: a slow chat list must not delay archiving
        assert source.index("register_handlers(client, self_id=me.id)") < start < source.index("await run_until_disconnected(client)")
        assert "read_sync_task.cancel()" in source[source.index("Shutting down"):]
