"""
Files of messages that were archived BEFORE media archiving existed: a full re-walk (backfill.py --full) gives them their 'pending' media row.

The incremental walk never revisits old messages (it starts after each chat's marker), so without this the files of existing history would never be recorded.
Telegram is faked; the archive and its rules are real.
"""

import pytest
import sqlalchemy as sa
from telethon import types

import db.queries as q
from tests.helpers import T0
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (shared fixture and helpers)


def photo(size=2048):
    sizes: list[types.TypePhotoSize] = [types.PhotoSize(type="x", w=1, h=1, size=size)]
    return types.MessageMediaPhoto(photo=types.Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=sizes, dc_id=1))


STICKER = types.MessageMediaDocument(
    document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="image/webp", size=10, dc_id=1,
        attributes=[types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())],
    )
)


def media_rows(conn):
    conn.rollback()
    rows = conn.execute(
        sa.text("SELECT m.tg_message_id, mm.kind, mm.status, mm.size_bytes FROM message_media mm JOIN messages m ON m.id = mm.message_id ORDER BY m.tg_message_id")
    )
    return [tuple(r) for r in rows]


def archive_text_only(conn, tg_id=1, text="caption"):
    """What an archive made before step 1 holds for a photo with a caption: the caption, and nothing that says a file existed."""
    q.upsert_chat(conn, chat_id=11, name="chat11", chat_type="private")
    q.insert_message(conn, tg_message_id=tg_id, chat_id=11, sender_id=None, text=text, date=T0)


def test_a_full_walk_records_the_file_of_a_message_that_was_already_archived(archive, telegram):  # noqa: F811
    archive_text_only(archive.conn)
    telegram({11: [("caption", photo(2048))]}, force_full=True)
    assert media_rows(archive.conn) == [(1, "photo", "pending", 2048)]
    assert stored(archive.conn) == {11: 1}  # the message itself was not duplicated


def test_the_archived_message_is_left_exactly_as_it_was(archive, telegram):  # noqa: F811
    archive_text_only(archive.conn, text="what the archive already had")
    telegram({11: [("what Telegram says now", photo())]}, force_full=True)
    archive.conn.rollback()
    assert archive.conn.execute(sa.text("SELECT text, is_edited FROM messages")).one() == ("what the archive already had", False)


def test_a_media_row_that_exists_is_never_reset(archive, telegram):  # noqa: F811
    archive_text_only(archive.conn)
    archive.conn.execute(sa.text("INSERT INTO message_media (message_id, kind, status, size_bytes, file_path) SELECT id, 'photo', 'stored', 5, 'users/1/media/7' FROM messages"))
    archive.conn.commit()
    telegram({11: [("caption", photo(2048))]}, force_full=True)
    assert media_rows(archive.conn) == [(1, "photo", "stored", 5)]


def test_messages_without_a_storable_file_get_no_row(archive, telegram):  # noqa: F811
    archive_text_only(archive.conn, 1)
    archive_text_only(archive.conn, 2)
    telegram({11: [("plain", None), ("sticker", STICKER)]}, force_full=True)
    assert media_rows(archive.conn) == []


def test_a_message_that_was_never_archived_is_stored_with_its_row_as_before(archive, telegram):  # noqa: F811
    telegram({11: [("new", photo(10))]}, force_full=True)
    assert media_rows(archive.conn) == [(1, "photo", "pending", 10)]


def test_an_old_message_keeps_its_row_when_the_walk_is_repeated(archive, telegram):  # noqa: F811
    archive_text_only(archive.conn)
    telegram({11: [("caption", photo(2048))]}, force_full=True)
    telegram({11: [("caption", photo(2048))]}, force_full=True)
    assert media_rows(archive.conn) == [(1, "photo", "pending", 2048)]


class TestAddMissingMediaRow:
    def test_says_whether_a_row_was_added(self, archive):
        archive_text_only(archive.conn)
        assert q.add_missing_media_row(archive.conn, 1, 11, "photo", 3) is True
        assert q.add_missing_media_row(archive.conn, 1, 11, "photo", 3) is False

    def test_a_message_the_archive_does_not_hold_is_not_invented(self, archive):
        archive_text_only(archive.conn)
        assert q.add_missing_media_row(archive.conn, 99, 11, "photo", 3) is False
        assert media_rows(archive.conn) == []


class TestRequeue:
    def seed(self, conn):
        conn.execute(sa.text("INSERT INTO chats (chat_id, name, chat_type) VALUES (11, 'c', 'private')"))
        for i, status in enumerate(["pending", "too_large", "disk_full", "failed", "unavailable", "stored"], start=1):
            q.insert_message(conn, tg_message_id=i, chat_id=11, sender_id=None, text=None, date=T0, media_kind="video", media_size=i * 100)
        for i, status in enumerate(["pending", "too_large", "disk_full", "failed", "unavailable", "stored"], start=1):
            path = "users/1/media/9" if status == "stored" else None
            conn.execute(sa.text("UPDATE message_media SET status = :s, file_path = :p WHERE message_id = (SELECT id FROM messages WHERE tg_message_id = :t)"), {"s": status, "p": path, "t": i})
        conn.commit()

    def statuses(self, conn):
        return [(t, s) for t, _, s, _ in media_rows(conn)]

    def test_refused_and_failed_files_go_back_in_the_queue_and_nothing_else_moves(self, archive):
        self.seed(archive.conn)
        assert q.requeue_media(archive.conn) == 3
        assert self.statuses(archive.conn) == [(1, "pending"), (2, "pending"), (3, "pending"), (4, "pending"), (5, "unavailable"), (6, "stored")]

    def test_only_the_asked_statuses_are_requeued(self, archive):
        self.seed(archive.conn)
        assert q.requeue_media(archive.conn, ["disk_full"]) == 1
        assert self.statuses(archive.conn) == [(1, "pending"), (2, "too_large"), (3, "pending"), (4, "failed"), (5, "unavailable"), (6, "stored")]

    def test_the_declared_size_is_kept_so_a_still_over_limit_file_is_refused_without_traffic(self, archive):
        self.seed(archive.conn)
        q.requeue_media(archive.conn)
        assert [size for *_, size in media_rows(archive.conn)] == [100, 200, 300, 400, 500, 600]

    @pytest.mark.parametrize("status", ["stored", "unavailable", "pending", "nonsense"])
    def test_a_status_that_must_not_be_retried_is_refused(self, archive, status):
        self.seed(archive.conn)
        with pytest.raises(ValueError):
            q.requeue_media(archive.conn, [status])
        assert self.statuses(archive.conn)[5] == (6, "stored")

    def test_an_empty_list_changes_nothing(self, archive):
        self.seed(archive.conn)
        assert q.requeue_media(archive.conn, []) == 0
