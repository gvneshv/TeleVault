"""
Backfill modes (all / text / media) and the two walk markers.

"all" fetches messages and files, "text" only text (a file is recorded as 'skipped'), "media" only messages that carry a file.
Each walk keeps its own marker - chats.backfill_covered_id for text, chats.media_walked_id for files - and "all" resumes from the lower of the two,
so a text-only run can never make a later media run skip the files it passed.
Telegram is faked; the archive is real.
"""

import pytest
import sqlalchemy as sa
from telethon import types

import db.queries as q
from tests.helpers import T0
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (shared fixture and helpers)
from tests.integration.test_backfill_media_rows import STICKER, archive_text_only, media_rows, photo

# Three messages: plain text, a photo with a caption, a photo with no caption (id 1, 2, 3).
HISTORY = {11: [("hello", None), ("look", photo(10)), (None, photo(20))]}


def markers(conn):
    conn.rollback()
    return tuple(conn.execute(sa.text("SELECT backfill_covered_id, media_walked_id FROM chats WHERE chat_id = 11")).one())


def texts(conn):
    conn.rollback()
    return conn.execute(sa.text("SELECT tg_message_id, text FROM messages ORDER BY tg_message_id")).all()


class TestWhatEachModeStores:
    def test_all_stores_text_and_files(self, archive, telegram):  # noqa: F811
        telegram(HISTORY)
        assert [tuple(r) for r in texts(archive.conn)] == [(1, "hello"), (2, "look"), (3, None)]
        assert media_rows(archive.conn) == [(2, "photo", "pending", 10), (3, "photo", "pending", 20)]

    def test_text_stores_only_messages_with_text_and_records_their_file_without_queueing_it(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        assert [tuple(r) for r in texts(archive.conn)] == [(1, "hello"), (2, "look")]
        assert media_rows(archive.conn) == [(2, "photo", "skipped", 10)]
        assert q.next_pending_media(archive.conn) is None

    def test_media_stores_only_messages_with_a_file(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="media")
        assert [tuple(r) for r in texts(archive.conn)] == [(2, "look"), (3, None)]
        assert media_rows(archive.conn) == [(2, "photo", "pending", 10), (3, "photo", "pending", 20)]

    def test_a_file_type_that_is_switched_off_is_skipped_in_every_mode(self, archive, telegram):  # noqa: F811
        q.save_media_policy(archive.conn, ["video"])
        telegram(HISTORY, mode="media")
        assert media_rows(archive.conn) == [(2, "photo", "skipped", 10), (3, "photo", "skipped", 20)]

    def test_messages_that_are_neither_text_nor_a_storable_file_are_passed_over_in_every_mode(self, archive, telegram):  # noqa: F811
        for mode in ("all", "text", "media"):
            telegram({11: [(None, STICKER)]}, mode=mode)
        assert stored(archive.conn) == {}


class TestExistingMessages:
    def test_media_gives_an_archived_message_its_row(self, archive, telegram):  # noqa: F811
        archive_text_only(archive.conn)
        telegram({11: [("caption", photo(5))]}, mode="media")
        assert media_rows(archive.conn) == [(1, "photo", "pending", 5)]

    def test_text_leaves_an_archived_message_without_a_row(self, archive, telegram):  # noqa: F811
        archive_text_only(archive.conn)
        telegram({11: [("caption", photo(5))]}, mode="text")
        assert media_rows(archive.conn) == []


class TestMarkers:
    def test_all_moves_both(self, archive, telegram):  # noqa: F811
        telegram(HISTORY)
        assert markers(archive.conn) == (3, 3)

    def test_text_moves_only_the_text_marker(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        assert markers(archive.conn) == (3, None)

    def test_media_moves_only_the_file_marker(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="media")
        assert markers(archive.conn) == (None, 3)

    def test_a_media_run_after_a_text_run_still_walks_the_whole_chat(self, archive, telegram):  # noqa: F811
        """The point of two markers: the text walk went past the photos without taking them."""
        telegram(HISTORY, mode="text")
        fake, _ = telegram(HISTORY, mode="media")
        assert [c["min_id"] for c in fake.iter_calls] == [0]
        assert media_rows(archive.conn) == [(2, "photo", "pending", 10), (3, "photo", "pending", 20)]  # message 2's row, set aside by the text run, is queued now

    def test_a_repeat_run_of_the_same_mode_is_incremental(self, archive, telegram):  # noqa: F811
        for mode in ("all", "text", "media"):
            telegram(HISTORY, mode=mode)
            fake, _ = telegram(HISTORY, mode=mode)
            assert [c["min_id"] for c in fake.iter_calls] == [3], mode

    def test_all_resumes_from_the_lower_marker(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")  # text marker 3, file marker unset
        archive.conn.execute(sa.text("UPDATE chats SET media_walked_id = 2 WHERE chat_id = 11"))
        archive.conn.commit()
        fake, _ = telegram(HISTORY, mode="all")
        assert [c["min_id"] for c in fake.iter_calls] == [2]

    def test_all_walks_everything_while_either_marker_is_unset(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        fake, _ = telegram(HISTORY, mode="all")
        assert [c["min_id"] for c in fake.iter_calls] == [0]

    def test_full_ignores_both(self, archive, telegram):  # noqa: F811
        telegram(HISTORY)
        fake, _ = telegram(HISTORY, force_full=True)
        assert [c["min_id"] for c in fake.iter_calls] == [0]


class TestUnmigratedArchive:
    @pytest.mark.parametrize("mode", ["text", "media"])
    def test_the_new_modes_refuse_to_start_and_fetch_nothing(self, archive, telegram, monkeypatch, mode):  # noqa: F811
        monkeypatch.setattr(q, "media_policy_ready", lambda conn: False)
        fakes = []
        with pytest.raises(SystemExit) as exit_info:
            telegram(HISTORY, mode=mode, setup=fakes.append)
        assert exit_info.value.code == 1
        assert fakes[0].fetched == []

    def test_all_still_works(self, archive, telegram, monkeypatch):  # noqa: F811
        monkeypatch.setattr(q, "media_policy_ready", lambda conn: False)
        telegram(HISTORY, mode="all")
        assert stored(archive.conn) == {11: 3}

    def test_an_unknown_mode_is_refused(self, archive, telegram):  # noqa: F811
        with pytest.raises(SystemExit):
            telegram(HISTORY, mode="everything")


class TestFilesSetAsideByATextRun:
    def test_a_later_files_run_queues_them(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        telegram(HISTORY, mode="all")
        assert [s for _, _, s, _ in media_rows(archive.conn)] == ["pending", "pending"]

    def test_unless_the_owner_switched_that_kind_off(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        q.save_media_policy(archive.conn, ["video"])
        telegram(HISTORY, mode="media")
        assert [s for _, _, s, _ in media_rows(archive.conn)] == ["skipped", "skipped"]

    def test_a_text_run_never_queues_anything(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="text")
        telegram(HISTORY, mode="text")
        assert [s for _, _, s, _ in media_rows(archive.conn)] == ["skipped"]

    def test_stored_and_refused_files_stay_as_they_are(self, archive, telegram):  # noqa: F811
        telegram(HISTORY, mode="all")
        archive.conn.execute(sa.text("UPDATE message_media SET status = 'too_large' WHERE kind = 'photo'"))
        archive.conn.commit()
        telegram(HISTORY, mode="media")
        assert {s for _, _, s, _ in media_rows(archive.conn)} == {"too_large"}


class TestSkipReasonsAreLogged:
    """A bare "skipped: 111" cannot tell "already archived" from "all older than the cleanup cutoff"; the log line says which."""

    def lines(self, caplog):
        return [r.getMessage() for r in caplog.records if "skipped" in r.getMessage() and r.getMessage().startswith("  skipped")]

    def test_the_reasons_are_counted(self, archive, telegram, caplog):  # noqa: F811
        caplog.set_level("INFO")
        telegram({11: [("a", None), (None, STICKER), ("b", None)]})
        telegram({11: [("a", None), (None, STICKER), ("b", None)]})
        assert self.lines(caplog) == [
            "  skipped 1: 1 nothing to store (sticker, GIF, service message...).",
            "  skipped 3: 2 already archived; 1 nothing to store (sticker, GIF, service message...).",
        ]

    def test_nothing_is_logged_when_nothing_was_skipped(self, archive, telegram, caplog):  # noqa: F811
        caplog.set_level("INFO")
        telegram({11: [("a", None)]})
        assert self.lines(caplog) == []

    def test_an_active_cleanup_policy_is_announced(self, archive, telegram, caplog):  # noqa: F811
        caplog.set_level("INFO")
        q.save_retention_policy(archive.conn, enabled=True, months=1, keep_deleted=True, confirmed_messages=0)
        telegram({11: [("a", None)]})
        assert any("Automatic cleanup is ON" in r.getMessage() for r in caplog.records)
