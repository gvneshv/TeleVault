"""
Downloaded files leave the disk with the messages they belong to: the database functions hand back the stored files' paths, the callers delete them.

Covers the three removal rules (cleanup, "delete this chat", "delete all data"), the start-up sweep of files nothing refers to, and what must NOT be removed:
files of messages that stay, pending/refused rows (there is no file), another chat's files.
"""

import shutil

import pytest
import sqlalchemy as sa

import db
import db.queries as q
import media_downloader
import retention
from tests.helpers import T0, seed_chat
from tests.integration.test_retention_db import OTHER, OWNER, age, remaining, save
from utils.worker_paths import media_dir, media_relative_path

USER = 1


@pytest.fixture(autouse=True)
def empty_media_folder():
    """The test DATA_DIR is shared by the whole session: start each test with no files of this account (the sweep tests count what they removed)."""
    shutil.rmtree(media_dir(USER), ignore_errors=True)
    yield
    shutil.rmtree(media_dir(USER), ignore_errors=True)


def store(conn, chat_id, tg_id, *, stored=True, user_id=USER):
    """A message with a photo row; when `stored`, the file exists on disk and the row says so.
    Returns (media_id, file or None)."""
    q.upsert_chat(conn, chat_id=chat_id, name=f"c{chat_id}", chat_type="private")
    q.insert_message(conn, tg_message_id=tg_id, chat_id=chat_id, sender_id=None, text=None, date=T0, media_kind="photo", media_size=3)
    media_id = conn.execute(
        sa.text("SELECT mm.id FROM message_media mm JOIN messages m ON m.id = mm.message_id WHERE m.chat_id = :c AND m.tg_message_id = :t"),
        {"c": chat_id, "t": tg_id},
    ).scalar()
    if not stored:
        return media_id, None
    path = media_relative_path(user_id, media_id)
    conn.execute(sa.text("UPDATE message_media SET status = 'stored', file_path = :p WHERE id = :i"), {"p": path, "i": media_id})
    conn.commit()
    file = media_dir(user_id) / str(media_id)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"abc")
    return media_id, file


class TestPathsComeBack:
    def test_purge_chat_returns_that_chats_stored_files_only(self, archive):
        mine, _ = store(archive.conn, 1, 1)
        store(archive.conn, 1, 2, stored=False)  # still pending: no file
        other, _ = store(archive.conn, 2, 1)
        result = q.purge_chat(archive.conn, 1)
        assert result is not None
        assert result["media_paths"] == [media_relative_path(USER, mine)]
        assert other not in [int(p.rsplit("/", 1)[1]) for p in result["media_paths"]]

    def test_clear_all_data_returns_every_stored_file(self, archive):
        a, _ = store(archive.conn, 1, 1)
        b, _ = store(archive.conn, 2, 1)
        store(archive.conn, 2, 2, stored=False)
        result = q.clear_all_data(archive.conn)
        assert sorted(result["media_paths"]) == sorted([media_relative_path(USER, a), media_relative_path(USER, b)])

    def test_a_purge_of_expired_messages_returns_only_the_files_of_the_ones_it_removed(self, archive):
        conn = archive.conn
        q.record_archive_owner(conn, OWNER)
        seed_chat(conn, chat_id=42, name="Bob", messages=[(i, OTHER, f"t{i}") for i in range(1, 4)])
        old, _ = store(conn, 42, 10)
        new, _ = store(conn, 42, 11)
        for tg_id, days in [(1, 400), (2, 300), (3, 1), (10, 400), (11, 1)]:
            age(conn, 42, tg_id, days)
        result = q.purge_expired_messages(conn, q.retention_cutoff(conn, 6), keep_deleted=True, limit=1000)
        assert result["media_paths"] == [media_relative_path(USER, old)]
        assert remaining(conn) == [3, 11]
        assert new  # its row (and so its file) is untouched
        assert conn.execute(sa.text("SELECT COUNT(*) FROM message_media")).scalar() == 1

    def test_nothing_to_return_on_an_archive_without_media_records(self, archive, monkeypatch):
        monkeypatch.setattr(q, "_message_media_ready", lambda conn: False)
        assert q._stored_media_paths(archive.conn) == []


class TestRetentionDeletesTheFiles:
    def seed(self, conn):
        q.record_archive_owner(conn, OWNER)
        seed_chat(conn, chat_id=42, name="Bob", messages=[(1, OTHER, "old")])
        _, old_file = store(conn, 42, 10)
        _, new_file = store(conn, 42, 11)
        age(conn, 42, 1, 400)
        age(conn, 42, 10, 400)
        age(conn, 42, 11, 1)
        save(conn)
        return old_file, new_file

    def test_the_file_of_an_expired_message_is_deleted_and_a_recent_one_stays(self, archive):
        old_file, new_file = self.seed(archive.conn)
        assert retention.apply_policy_once(archive.conn, user_id=USER) == "ok"
        assert old_file is not None and new_file is not None
        assert not old_file.exists()
        assert new_file.exists()

    def test_without_an_account_id_the_file_stays_for_the_sweep(self, archive):
        old_file, _ = self.seed(archive.conn)
        retention.apply_policy_once(archive.conn)
        assert old_file is not None and old_file.exists()


class TestStartupSweep:
    @pytest.fixture
    def connection(self, archive, monkeypatch):
        from contextlib import nullcontext

        monkeypatch.setattr(db, "get_connection", lambda: nullcontext(archive.conn))
        return archive.conn

    def test_files_nothing_refers_to_are_removed_and_referenced_ones_stay(self, connection):
        _, kept = store(connection, 1, 1)
        orphan = media_dir(USER) / "999999"
        partial = media_dir(USER) / "999998.partial"
        orphan.write_bytes(b"x")
        partial.write_bytes(b"x")
        assert media_downloader._sweep_orphans(USER) == 2
        assert kept is not None and kept.exists()
        assert not orphan.exists() and not partial.exists()

    def test_a_pending_rows_file_left_by_a_crash_is_removed_too(self, connection):
        """Downloaded and renamed, but the row was never updated: the row is still 'pending' and will fetch the file again."""
        media_id, _ = store(connection, 1, 1, stored=False)
        file = media_dir(USER) / str(media_id)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x")
        media_downloader._sweep_orphans(USER)
        assert not file.exists()


class TestLoopSweepsFirst:
    def test_the_downloader_sweeps_before_its_first_download(self, monkeypatch):
        import asyncio

        order: list[str] = []
        monkeypatch.setattr(media_downloader, "_sweep_orphans", lambda user_id: order.append("sweep") or 0)

        async def first_download(client, user_id):
            order.append("download")
            raise asyncio.CancelledError

        monkeypatch.setattr(media_downloader, "process_next", first_download)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(media_downloader.media_loop(object(), USER))
        assert order == ["sweep", "download"]