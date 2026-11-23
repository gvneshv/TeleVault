"""
Media-only messages (a photo, voice note or file with no caption) against a real archive database.

Before media archiving a message with no text was dropped.
Now it is stored with text NULL and a 'pending' message_media row,
so it counts in its chat, shows up in the chat view with the kind of file it carries, and the download queue (a later step) finds it.
Only Telegram is faked; the SQL, the counter triggers and the read queries are the real ones.
"""

from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from telethon import types

import db
import db.queries as q
import db.read_queries as rq
from handlers import on_edit, on_message
from tests.helpers import T0, seed_chat
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401

PHOTO = types.MessageMediaPhoto(photo=types.Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
STICKER = types.MessageMediaDocument(
    document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="image/webp", size=10, dc_id=1,
        attributes=[types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())],
    )
)


def media_rows(conn):
    conn.rollback()  # see rows committed from another connection (backfill.run() uses its own)
    return conn.execute(sa.text("SELECT m.tg_message_id, mm.kind, mm.status, mm.size_bytes FROM message_media mm JOIN messages m ON m.id = mm.message_id ORDER BY 1")).all()


def chat(conn):
    conn.rollback()
    return conn.execute(sa.text("SELECT message_count, last_message_preview FROM chats WHERE chat_id = 11")).one()


def add(conn, tg_id, text=None, **media):
    return q.insert_message(conn, tg_message_id=tg_id, chat_id=11, sender_id=None, text=text, date=T0, **media)


@pytest.fixture
def chat_11(archive):
    q.upsert_chat(archive.conn, chat_id=11, name="Alice", chat_type="private")
    return archive.conn


class TestInsert:
    def test_a_media_only_message_gets_a_pending_media_row(self, chat_11):
        add(chat_11, 1, media_kind="photo", media_size=2048)
        assert media_rows(chat_11) == [(1, "photo", "pending", 2048)]

    def test_the_size_may_be_unknown(self, chat_11):
        add(chat_11, 1, media_kind="photo")
        assert media_rows(chat_11) == [(1, "photo", "pending", None)]

    def test_a_plain_message_gets_no_media_row(self, chat_11):
        add(chat_11, 1, text="hello")
        assert media_rows(chat_11) == []

    def test_a_caption_and_a_media_row_live_side_by_side(self, chat_11):
        add(chat_11, 1, text="look", media_kind="video")
        assert media_rows(chat_11) == [(1, "video", "pending", None)]
        assert chat(chat_11).last_message_preview == "look"

    def test_a_redelivered_message_does_not_reset_its_media_row(self, chat_11):
        message_id = add(chat_11, 1, media_kind="photo")
        chat_11.execute(
            sa.text("UPDATE message_media SET status = 'stored', file_path = 'users/1/media/a' WHERE message_id = :m"), {"m": message_id}
        )
        chat_11.commit()
        assert add(chat_11, 1, media_kind="photo") is None
        assert media_rows(chat_11) == [(1, "photo", "stored", None)]

    def test_an_unknown_kind_is_refused_by_the_database_and_nothing_is_stored(self, chat_11):
        with pytest.raises(IntegrityError):
            add(chat_11, 1, media_kind="gif")
        chat_11.rollback()
        assert chat_11.execute(sa.text("SELECT COUNT(*) FROM messages")).scalar() == 0

    def test_an_archive_without_the_media_table_still_archives_the_message(self, chat_11, monkeypatch):
        monkeypatch.setattr(q, "_message_media_ready", lambda conn: False)
        assert add(chat_11, 1, media_kind="photo") is not None
        assert media_rows(chat_11) == []
        assert chat(chat_11).message_count == 1


class TestCounters:
    def test_a_media_only_message_counts_in_its_chat(self, chat_11):
        add(chat_11, 1, text="hi")
        add(chat_11, 2, media_kind="photo")
        assert chat(chat_11).message_count == 2

    def test_the_preview_of_a_chat_whose_newest_message_is_media_only_is_empty(self, chat_11):
        add(chat_11, 1, text="hi")
        q.insert_message(chat_11, tg_message_id=2, chat_id=11, sender_id=None, text=None, date=T0.replace(minute=5), media_kind="photo")
        # The chat list then says "Media" (web/js/views/chat-view.js) rather than showing the older text as if it were the last message.
        assert chat(chat_11).last_message_preview is None


class TestReadSide:
    def test_every_message_read_carries_its_kind(self, chat_11):
        add(chat_11, 1, text="plain")
        add(chat_11, 2, media_kind="voice")
        feed = {m["tg_message_id"]: m["media_kind"] for m in rq.get_messages(chat_11)["items"]}
        in_chat = {m["tg_message_id"]: m["media_kind"] for m in rq.get_chat_messages(chat_11, chat_id=11)["items"]}
        assert feed == in_chat == {1: None, 2: "voice"}

    def test_the_window_and_the_detail_carry_it_too(self, chat_11):
        message_id = add(chat_11, 1, media_kind="document")
        window = rq.get_chat_window(chat_11, chat_id=11, limit=10)
        assert window is not None
        assert [m["media_kind"] for m in window["items"]] == ["document"]
        assert message_id is not None
        detail = rq.get_message_detail(chat_11, message_id)
        assert detail is not None and detail["media_kind"] == "document" and detail["text"] is None

    def test_an_archive_without_the_media_table_reads_as_no_media(self, chat_11, monkeypatch):
        add(chat_11, 1, text="plain")
        monkeypatch.setattr(rq, "_message_media_ready", lambda conn: False)
        assert [m["media_kind"] for m in rq.get_chat_messages(chat_11, chat_id=11)["items"]] == [None]


class TestHandlersWrite:
    @pytest.fixture
    def archive_connection(self, archive, monkeypatch):
        @contextmanager
        def borrowed():
            yield archive.conn

        monkeypatch.setattr(db, "get_connection", borrowed)
        return archive.conn

    def persist(self, **overrides):
        args = dict(
            chat_id=11, chat_name="Alice", chat_type="private", chat_username=None, sender_id=None, username=None, first_name=None,
            last_name=None, tg_message_id=1, text=None, date=T0, media_kind="photo", media_size=10,
        )
        on_message._persist_message(**{**args, **overrides})

    def test_the_live_path_stores_a_media_only_message(self, archive_connection):
        self.persist()
        assert media_rows(archive_connection) == [(1, "photo", "pending", 10)]
        assert chat(archive_connection).message_count == 1

    def test_an_edit_of_a_media_message_we_never_saw_archives_it(self, archive_connection):
        on_edit._persist_edit(
            chat_id=11, chat_name="Alice", chat_type="private", chat_username=None, sender_id=None, username=None, first_name=None,
            last_name=None, tg_message_id=1, new_text=None, edited_at=T0, date=T0, media_kind="photo", media_size=None,
        )
        assert media_rows(archive_connection) == [(1, "photo", "pending", None)]
        assert archive_connection.execute(sa.text("SELECT is_edited FROM messages")).scalar() is True

    def test_a_captionless_edit_of_an_archived_message_changes_nothing(self, archive_connection):
        seed_chat(archive_connection, 11, "Alice", messages=[(1, None, "caption")])
        on_edit._persist_edit(
            chat_id=11, chat_name="Alice", chat_type="private", chat_username=None, sender_id=None, username=None, first_name=None,
            last_name=None, tg_message_id=1, new_text=None, edited_at=T0, date=T0, media_kind="photo", media_size=None,
        )
        assert archive_connection.execute(sa.text("SELECT text FROM messages")).scalar() == "caption"
        assert archive_connection.execute(sa.text("SELECT COUNT(*) FROM message_edits")).scalar() == 0


class TestBackfill:
    def test_a_media_only_message_is_stored_with_its_media_row(self, archive, telegram):  # noqa: F811
        telegram({11: ["hello", ("", PHOTO)]})
        assert stored(archive.conn) == {11: 2}
        assert media_rows(archive.conn) == [(2, "photo", "pending", None)]

    def test_a_sticker_alone_is_still_skipped(self, archive, telegram):  # noqa: F811
        telegram({11: ["hello", ("", STICKER)]})
        assert stored(archive.conn) == {11: 1}
        assert media_rows(archive.conn) == []

    def test_a_captioned_photo_gets_its_media_row_too(self, archive, telegram):  # noqa: F811
        telegram({11: [("a caption", PHOTO)]})
        assert media_rows(archive.conn) == [(1, "photo", "pending", None)]