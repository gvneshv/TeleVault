"""
The owner's local "Dismiss" of the unread bar, at the database level.

Telegram's own reading position can stay behind for good (a message deleted before it was read may leave it short of that message),
so the owner can tell TeleVault to treat everything shown as read.
What these tests pin: the stored number is clamped and only moves forward, the bar and the badge use the LARGER of it and Telegram's position,
it never touches Telegram's own number, and an archive without the column keeps working.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT
from tests.helpers import seed_chat

REVISION = "e4a8c2b6d913"
PREVIOUS_REVISION = "d7b3f9a2e154"
OWNER = 1
OTHER = 50


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def dismissed(conn, chat_id=42):
    """The raw stored dismissal (not the effective position)."""
    return scalar(conn, "SELECT unread_dismissed_id FROM chats WHERE chat_id = :c", c=chat_id)


def telegram_position(conn, chat_id=42):
    """Telegram's own number, as the archiver stored it."""
    return scalar(conn, "SELECT read_inbox_max_id FROM chats WHERE chat_id = :c", c=chat_id)


def view(conn, chat_id=42):
    """The chat record as the API serves it: effective position and badge."""
    chat = rq.get_chat(conn, chat_id)
    assert chat is not None
    return chat["read_inbox_max_id"], chat["unread_count"]


@pytest.fixture
def chat(archive):
    """A chat with messages 1..6; the owner wrote 2 and 5. Owner known, Telegram says everything up to 3 is read."""
    q.record_archive_owner(archive.conn, OWNER)
    msgs = [(1, OTHER, "a"), (2, OWNER, "b"), (3, OTHER, "c"), (4, OTHER, "d"), (5, OWNER, "e"), (6, OTHER, "f")]
    seed_chat(archive.conn, chat_id=42, name="Bob", messages=msgs)
    q.record_read_inbox(archive.conn, 42, 3)
    return archive.conn


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_this_revision_follows_the_previous_one_in_a_single_chain(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_a_provisioned_archive_has_the_column_and_it_starts_empty(self, chat):
        assert q.chats_have_unread_dismissal(chat) is True
        assert dismissed(chat) is None

    def test_the_declared_model_has_the_column(self):
        """db/schema.py is the model the code reads; a drift from the migration would go unnoticed until someone autogenerates."""
        from db.schema import chats

        assert "unread_dismissed_id" in chats.c
        assert isinstance(chats.c.unread_dismissed_id.type, sa.BigInteger)

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_104).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)

        def has_column(conn):
            return bool(
                scalar(
                    conn,
                    "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('public.chats') "
                    "AND attname = 'unread_dismissed_id' AND NOT attisdropped)",
                )
            )

        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert not has_column(conn)
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert has_column(conn)
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()


# ---------------------------------------------------------------------------
# Dismissing
# ---------------------------------------------------------------------------


class TestDismiss:
    def test_dismissing_the_newest_message_clears_the_badge_and_moves_the_position(self, chat):
        assert view(chat) == (3, 2)  # 4 and 6 are unread; 5 is the owner's own
        assert q.dismiss_unread(chat, 42, 6) is True
        assert view(chat) == (6, 0)

    def test_a_partial_dismissal_leaves_what_came_after_unread(self, chat):
        q.dismiss_unread(chat, 42, 4)
        assert view(chat) == (4, 1)  # only 6

    def test_telegrams_own_number_is_never_touched(self, chat):
        q.dismiss_unread(chat, 42, 6)
        assert telegram_position(chat) == 3
        assert dismissed(chat) == 6

    def test_the_larger_of_the_two_wins_when_telegram_catches_up(self, chat):
        q.dismiss_unread(chat, 42, 4)
        q.record_read_inbox(chat, 42, 6)
        assert view(chat) == (6, 0)

    def test_telegram_ahead_of_the_dismissal_wins(self, chat):
        q.record_read_inbox(chat, 42, 5)
        q.dismiss_unread(chat, 42, 4)
        assert view(chat) == (5, 1)  # Telegram's 5 stays the position; only 6 is unread

    def test_a_dismissal_alone_counts_when_telegram_has_reported_nothing(self, archive):
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "b"), (3, OTHER, "c")])
        assert view(archive.conn) == (None, None)
        q.dismiss_unread(archive.conn, 42, 2)
        assert view(archive.conn) == (2, 1)

    def test_it_is_clamped_to_the_newest_archived_message(self, chat):
        """A number beyond the newest message would pre-dismiss messages that have not arrived yet."""
        q.dismiss_unread(chat, 42, 10**12)
        assert dismissed(chat) == 6
        seed_chat(chat, chat_id=42, name="Bob", messages=[(7, OTHER, "later")])
        assert view(chat) == (6, 1)  # the message that arrived afterwards is unread, as usual

    def test_it_only_moves_forward(self, chat):
        q.dismiss_unread(chat, 42, 6)
        q.dismiss_unread(chat, 42, 3)
        assert dismissed(chat) == 6

    def test_dismissing_twice_is_harmless(self, chat):
        assert q.dismiss_unread(chat, 42, 6) is True
        assert q.dismiss_unread(chat, 42, 6) is True
        assert view(chat) == (6, 0)

    def test_an_unknown_chat_is_reported_and_nothing_is_written(self, chat):
        assert q.dismiss_unread(chat, 999, 6) is None
        assert dismissed(chat) is None

    def test_a_chat_without_archived_messages_has_nothing_to_dismiss(self, archive):
        q.upsert_chat(archive.conn, chat_id=42, name="Empty", chat_type="private", username=None)
        assert q.dismiss_unread(archive.conn, 42, 5) is True
        assert dismissed(archive.conn) is None

    def test_chats_are_independent(self, archive):
        """Message ids of different chats overlap; a dismissal belongs to one chat only."""
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "b")])
        seed_chat(archive.conn, chat_id=43, name="Eve", messages=[(1, OTHER, "a"), (2, OTHER, "b")])
        q.record_read_inbox(archive.conn, 42, 0)
        q.record_read_inbox(archive.conn, 43, 0)
        q.dismiss_unread(archive.conn, 42, 2)
        assert view(archive.conn, 42) == (2, 0)
        assert view(archive.conn, 43) == (0, 2)
        assert dismissed(archive.conn, 43) is None

    def test_the_list_and_a_single_chat_agree(self, chat):
        q.dismiss_unread(chat, 42, 4)
        item = rq.get_chats(chat)["items"][0]
        assert (item["read_inbox_max_id"], item["unread_count"]) == view(chat) == (4, 1)

    def test_a_deleted_message_before_the_dismissal_no_longer_pins_the_bar(self, archive):
        """The reported case: a message deleted before it was read; Telegram's number stays behind it."""
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "gone"), (3, OTHER, "c")], deleted=[2])
        q.record_read_inbox(archive.conn, 42, 1)
        assert view(archive.conn) == (1, 2)
        q.dismiss_unread(archive.conn, 42, 3)
        assert view(archive.conn) == (3, 0)


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def without_dismissal(server_ready):
    """A real archive that has the read position but not the dismissal column (every archive before this change)."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_105).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("ALTER TABLE chats DROP COLUMN unread_dismissed_id"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


def test_an_archive_without_the_column_keeps_showing_telegrams_own_position(without_dismissal):
    conn = without_dismissal
    q.record_archive_owner(conn, OWNER)
    seed_chat(conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "b"), (3, OTHER, "c")])
    q.record_read_inbox(conn, 42, 1)
    assert q.chats_have_unread_dismissal(conn) is False
    assert view(conn) == (1, 2)
    assert [c["unread_count"] for c in rq.get_chats(conn)["items"]] == [2]
