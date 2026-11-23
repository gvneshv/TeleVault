"""
media_downloader.py against a real archive database and a fake Telegram: the queue, the limits, flood waits and every way a download can end.

Only Telegram is faked.
The rows, the status rules (CHECK constraints) and the files on disk are real; the data directory is a throwaway folder per test.
"""

import asyncio
import errno
from collections.abc import Callable
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from telethon import types
from telethon.errors import FloodWaitError

import db
import db.queries as q
import media_downloader as md
from tests.helpers import T0
from utils import worker_paths

USER = 1
MB = 1024 * 1024

PHOTO = types.MessageMediaPhoto(photo=types.Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))


def file_media(*attributes, size=1000):
    document = types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=size, dc_id=1, attributes=list(attributes)
    )
    return types.MessageMediaDocument(document=document)


VIDEO = types.DocumentAttributeVideo(duration=1, w=1, h=1)
STICKER = types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())


def telegram_message(media, mime="video/mp4"):
    """What client.get_messages() returns: a message-shaped object carrying REAL Telethon media (the classifier is not faked)."""
    return SimpleNamespace(media=media, ttl_period=None, file=SimpleNamespace(mime_type=mime))


class FakeClient:
    """A Telegram stand-in: `messages` maps tg_message_id -> message (or None for 'gone'), `payload` is what a download writes."""

    def __init__(self, messages, payload=b"x" * 100):
        self.messages = messages
        self.payload = payload
        self.requested: list[int] = []
        self.downloads = 0
        self.fail_with: Exception | None = None  # raised by get_messages
        self.download_error: BaseException | None = None  # raised by download_media AFTER writing half the file (BaseException: a shutdown is a CancelledError)
        self.during_download: Callable[[], None] | None = None  # run in the middle of a download

    async def get_messages(self, chat_id, ids):
        self.requested.append(ids)
        if self.fail_with:
            raise self.fail_with
        return self.messages.get(ids)

    async def download_media(self, message, file):
        self.downloads += 1
        with open(file, "wb") as out:
            out.write(self.payload[: len(self.payload) // 2])
        if self.during_download:
            self.during_download()
        if self.download_error:
            raise self.download_error
        with open(file, "wb") as out:
            out.write(self.payload)
        return file


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_paths, "settings", SimpleNamespace(data_dir=str(tmp_path)))
    return tmp_path


@pytest.fixture
def conn(archive, monkeypatch):
    @contextmanager
    def borrowed():
        yield archive.conn

    monkeypatch.setattr(db, "get_connection", borrowed)
    q.upsert_chat(archive.conn, chat_id=11, name="Alice", chat_type="private")
    return archive.conn


def enqueue(conn, tg_id, kind="video", size=None):
    """Archive a media-only message the way the live handler does; returns its message_media id."""
    q.insert_message(conn, tg_message_id=tg_id, chat_id=11, sender_id=None, text=None, date=T0, media_kind=kind, media_size=size)
    return conn.execute(sa.text("SELECT mm.id FROM message_media mm JOIN messages m ON m.id = mm.message_id WHERE m.tg_message_id = :t"), {"t": tg_id}).scalar_one()


def row(conn, media_id):
    conn.rollback()
    return conn.execute(sa.text("SELECT status, file_path, mime_type, size_bytes FROM message_media WHERE id = :i"), {"i": media_id}).one()


def run(client, **limits):
    return asyncio.run(md.process_next(client, USER, **limits))


def files(data_dir):
    folder = data_dir / "users" / str(USER) / "media"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


class TestRefusalRule:
    @pytest.mark.parametrize(
        "size, stored, expected",
        [
            (None, 0, None),  # unknown: decided later, on the real size
            (50, 0, None),  # exactly at the per-file cap: allowed
            (51, 0, "too_large"),
            (10, 90, None),  # exactly fills the account: allowed
            (11, 90, "disk_full"),
            (51, 10_000, "too_large"),  # the per-file cap wins over the disk limit
        ],
    )
    def test_boundaries(self, size, stored, expected):
        assert md.refusal(size, stored, max_file_bytes=50, account_limit_bytes=100) == expected


class TestSuccess:
    def test_a_pending_file_is_downloaded_and_recorded(self, conn, data_dir):
        media_id = enqueue(conn, 5, size=100)
        client = FakeClient({5: telegram_message(file_media(VIDEO, size=100))})
        outcome = run(client)
        assert outcome == md.Outcome("stored", True)
        status, path, mime, size = row(conn, media_id)
        assert (status, path, mime, size) == ("stored", f"users/{USER}/media/{media_id}", "video/mp4", 100)
        assert (data_dir / path).read_bytes() == b"x" * 100
        assert files(data_dir) == [str(media_id)]  # no .partial left behind

    def test_the_recorded_size_is_the_real_one_not_the_declared_one(self, conn, data_dir):
        media_id = enqueue(conn, 5, size=999)
        run(FakeClient({5: telegram_message(file_media(VIDEO, size=999))}, payload=b"y" * 40))
        assert row(conn, media_id).size_bytes == 40

    def test_a_photo_with_no_declared_size_is_fetched_too(self, conn, data_dir):
        media_id = enqueue(conn, 5, kind="photo")
        run(FakeClient({5: telegram_message(PHOTO, mime="image/jpeg")}))
        assert row(conn, media_id).status == "stored"

    def test_nothing_is_downloaded_for_an_empty_queue(self, conn, data_dir):
        client = FakeClient({})
        assert run(client) is None
        assert client.requested == []

    def test_the_oldest_job_goes_first(self, conn, data_dir):
        first = enqueue(conn, 5)
        second = enqueue(conn, 6)
        media = {5: telegram_message(file_media(VIDEO)), 6: telegram_message(file_media(VIDEO))}
        run(FakeClient(media))
        assert row(conn, first).status == "stored" and row(conn, second).status == "pending"

    def test_an_already_settled_row_is_not_fetched_again(self, conn, data_dir):
        enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})
        run(client)
        assert run(client) is None and client.downloads == 1


class TestLimits:
    def test_a_file_over_the_cap_is_refused_without_asking_telegram(self, conn, data_dir):
        media_id = enqueue(conn, 5, size=60 * MB)
        client = FakeClient({5: telegram_message(file_media(VIDEO, size=60 * MB))})
        outcome = run(client, max_file_bytes=50 * MB)
        assert outcome == md.Outcome("too_large", False)
        assert row(conn, media_id).status == "too_large" and row(conn, media_id).size_bytes == 60 * MB
        assert client.requested == [] and files(data_dir) == []

    def test_the_size_telegram_declares_now_counts_when_none_was_recorded(self, conn, data_dir):
        media_id = enqueue(conn, 5)  # recorded without a size
        client = FakeClient({5: telegram_message(file_media(VIDEO, size=60 * MB))})
        outcome = run(client, max_file_bytes=50 * MB)
        assert outcome == md.Outcome("too_large", True)
        assert row(conn, media_id).status == "too_large" and client.downloads == 0

    def test_a_photo_that_turns_out_too_big_is_discarded_after_download(self, conn, data_dir):
        media_id = enqueue(conn, 5, kind="photo")
        client = FakeClient({5: telegram_message(PHOTO)}, payload=b"z" * 100)
        run(client, max_file_bytes=50)
        assert row(conn, media_id).status == "too_large" and files(data_dir) == []

    def test_the_account_limit_refuses_a_file_that_would_not_fit(self, conn, data_dir):
        enqueue(conn, 5, size=60)
        first_media = {5: telegram_message(file_media(VIDEO, size=60))}
        run(FakeClient(first_media, payload=b"a" * 60), account_limit_bytes=100)
        second = enqueue(conn, 6, size=60)
        client = FakeClient({6: telegram_message(file_media(VIDEO, size=60))})
        outcome = run(client, account_limit_bytes=100)
        assert outcome == md.Outcome("disk_full", False)
        assert row(conn, second).status == "disk_full" and client.requested == []

    def test_a_real_disk_that_is_full_is_recorded_as_disk_full(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})
        client.download_error = OSError(errno.ENOSPC, "No space left on device")
        run(client)
        assert row(conn, media_id).status == "disk_full" and files(data_dir) == []


class TestEndings:
    def test_a_message_deleted_on_telegram_is_unavailable(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        assert run(FakeClient({5: None})) == md.Outcome("unavailable", True)
        assert row(conn, media_id).status == "unavailable"

    def test_a_message_that_became_a_sticker_is_unavailable(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        run(FakeClient({5: telegram_message(file_media(STICKER))}))
        assert row(conn, media_id).status == "unavailable"

    def test_a_message_that_became_timed_is_not_kept(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        message = telegram_message(file_media(VIDEO))
        message.ttl_period = 60
        run(FakeClient({5: message}))
        assert row(conn, media_id).status == "unavailable" and files(data_dir) == []

    def test_a_failed_download_is_recorded_and_leaves_no_partial_file(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})
        client.download_error = RuntimeError("connection reset")
        assert run(client) == md.Outcome("failed", True)
        assert row(conn, media_id).status == "failed" and files(data_dir) == []

    def test_a_flood_wait_leaves_the_job_pending(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({})
        client.fail_with = FloodWaitError(request=None, capture=30)
        with pytest.raises(FloodWaitError):
            run(client)
        assert row(conn, media_id).status == "pending"

    def test_a_message_deleted_from_the_archive_mid_download_leaves_no_file(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})

        def purge():  # the retention job (or the owner) removes the message while its file is downloading
            other = conn.engine.connect()
            other.execute(sa.text("DELETE FROM messages WHERE tg_message_id = 5"))
            other.commit()
            other.close()

        client.during_download = purge
        run(client)
        conn.rollback()
        assert conn.execute(sa.text("SELECT COUNT(*) FROM message_media")).scalar() == 0
        assert files(data_dir) == []

    def test_shutting_down_mid_download_leaves_no_partial_file(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})
        client.download_error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            run(client)
        assert row(conn, media_id).status == "pending" and files(data_dir) == []

    def test_an_archive_without_the_media_table_has_an_empty_queue(self, conn, data_dir, monkeypatch):
        monkeypatch.setattr(q, "_message_media_ready", lambda c: False)
        assert run(FakeClient({})) is None


class TestLoop:
    """media_loop() with a recording `sleep`; a CancelledError from sleep ends it, as main.py's shutdown does."""

    def drive(self, client, stop_after):
        sleeps: list[float] = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= stop_after:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(md.media_loop(client, USER, sleep=sleep))
        return sleeps

    def test_it_pauses_after_a_download_and_polls_when_idle(self, conn, data_dir):
        enqueue(conn, 5)
        sleeps = self.drive(FakeClient({5: telegram_message(file_media(VIDEO))}), stop_after=2)
        assert sleeps == [md.PACING_SECONDS, md.IDLE_POLL_SECONDS]

    def test_a_flood_wait_is_obeyed_plus_a_margin_then_the_job_is_retried(self, conn, data_dir):
        media_id = enqueue(conn, 5)
        client = FakeClient({5: telegram_message(file_media(VIDEO))})
        client.fail_with = FloodWaitError(request=None, capture=30)
        calls = {"n": 0}
        original = client.get_messages

        async def flaky(chat_id, ids):
            calls["n"] += 1
            if calls["n"] == 2:
                client.fail_with = None  # Telegram let us through the second time
            return await original(chat_id, ids)

        client.get_messages = flaky
        sleeps = self.drive(client, stop_after=3)
        assert sleeps[0] == 30 + md.FLOOD_MARGIN_SECONDS
        assert row(conn, media_id).status == "stored"

    def test_a_refusal_that_costs_no_traffic_is_not_followed_by_a_pause(self, conn, data_dir):
        enqueue(conn, 5, size=60 * MB)  # the default cap (config.py) is 50 MB, so this is refused from the recorded size alone
        client = FakeClient({})
        sleeps = self.drive(client, stop_after=1)
        assert sleeps == [md.IDLE_POLL_SECONDS]  # straight on to the next look: the queue was empty after the refusal
        assert client.requested == []

    def test_a_database_outage_skips_the_pass_instead_of_ending_the_loop(self, conn, data_dir, monkeypatch):
        def down():
            raise RuntimeError("db down")

        monkeypatch.setattr(md, "_next_job", down)
        assert self.drive(FakeClient({}), stop_after=2) == [md.IDLE_POLL_SECONDS] * 2


class TestFinishMedia:
    def test_a_settled_row_is_never_overwritten(self, conn):
        media_id = enqueue(conn, 5)
        assert q.finish_media(conn, media_id, "unavailable") is True
        assert q.finish_media(conn, media_id, "stored", file_path="users/1/media/x") is False
        assert row(conn, media_id).status == "unavailable"

    def test_a_vanished_row_reports_false(self, conn):
        assert q.finish_media(conn, 424242, "failed") is False

    def test_a_refusal_keeps_the_size_telegram_declared(self, conn):
        media_id = enqueue(conn, 5, size=77)
        q.finish_media(conn, media_id, "too_large")
        assert row(conn, media_id).size_bytes == 77

    def test_the_disk_usage_counts_only_stored_files(self, conn):
        stored = enqueue(conn, 5, size=10)
        refused = enqueue(conn, 6, size=20)
        q.finish_media(conn, stored, "stored", file_path="users/1/media/a", size_bytes=10)
        q.finish_media(conn, refused, "too_large")
        assert q.stored_media_bytes(conn) == 10