"""The chat list's unread badge at the database level: what is counted, what is not, the cap, and the index it relies on."""

import contextlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT
from tests.helpers import seed_chat

REVISION = "d7b3f9a2e154"
PREVIOUS_REVISION = "c5e9a1d3f742"
OWNER = 1
OTHER = 50


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


@contextlib.contextmanager
def recorded_sql(conn):
    statements: list[str] = []

    def listener(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    event.listen(conn.engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(conn.engine, "before_cursor_execute", listener)


def unread(conn, chat_id=42):
    return rq.get_chat(conn, chat_id)["unread_count"]  # type: ignore[index]


@pytest.fixture
def chat(archive):
    """A chat with messages 1..6; the owner wrote 2 and 5.
    The owner is known; the read position is not set yet."""
    q.record_archive_owner(archive.conn, OWNER)
    msgs = [(1, OTHER, "a"), (2, OWNER, "b"), (3, OTHER, "c"), (4, OTHER, "d"), (5, OWNER, "e"), (6, OTHER, "f")]
    seed_chat(archive.conn, chat_id=42, name="Bob", messages=msgs)
    return archive.conn


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestIndex:
    def test_this_revision_follows_the_previous_one_in_a_single_chain(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_a_provisioned_archive_has_the_index_with_chat_id_first(self, archive):
        cols = archive.conn.execute(
            sa.text(
                "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
                "WHERE c.relname = 'idx_messages_chat_tg_id' ORDER BY array_position(i.indkey::int2[], a.attnum)"
            )
        ).scalars().all()
        assert cols == ["chat_id", "tg_message_id"]

    def test_the_declared_model_matches_the_migration(self):
        """db/schema.py is the model the code reads; a drift from the migration would go unnoticed until someone autogenerates."""
        from db.schema import messages

        declared = {str(ix.name): [c.name for c in ix.columns] for ix in messages.indexes}
        assert declared["idx_messages_chat_tg_id"] == ["chat_id", "tg_message_id"]

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        import os

        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_102).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.idx_messages_chat_tg_id')") is None
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.idx_messages_chat_tg_id')") is not None
            eng.dispose()
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]


# ---------------------------------------------------------------------------
# What is counted
# ---------------------------------------------------------------------------


class TestCounting:
    def test_messages_after_the_position_that_somebody_else_wrote(self, chat):
        q.record_read_inbox(chat, 42, 3)
        assert unread(chat) == 2  # 4 and 6; 5 is the owner's own

    def test_the_owners_own_messages_are_never_unread(self, chat):
        q.record_read_inbox(chat, 42, 4)
        assert unread(chat) == 1  # only 6, not 5

    def test_everything_read_is_zero_not_unknown(self, chat):
        q.record_read_inbox(chat, 42, 6)
        assert unread(chat) == 0

    def test_nothing_read_yet_counts_every_message_of_somebody_else(self, chat):
        q.record_read_inbox(chat, 42, 0)
        assert unread(chat) == 4  # 1, 3, 4, 6

    def test_the_message_at_the_position_is_read(self, chat):
        q.record_read_inbox(chat, 42, 4)
        q.record_read_inbox(chat, 42, 4)
        assert unread(chat) == 1

    def test_unknown_position_means_unknown_not_zero(self, chat):
        assert unread(chat) is None

    def test_unknown_owner_means_unknown(self, archive):
        """Without the owner's id their own messages cannot be left out, so any number would be wrong."""
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OWNER, "b")])
        q.record_read_inbox(archive.conn, 42, 0)
        assert unread(archive.conn) is None

    def test_a_deleted_message_still_counts(self, archive):
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "b")], deleted=[2])
        q.record_read_inbox(archive.conn, 42, 1)
        assert unread(archive.conn) == 1

    def test_a_message_without_a_sender_counts(self, archive):
        """Channel posts have no sender; NULL must not be mistaken for 'the owner'."""
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="News", chat_type="channel", messages=[(1, None, "a"), (2, None, "b")])
        q.record_read_inbox(archive.conn, 42, 1)
        assert unread(archive.conn) == 1

    def test_chats_do_not_leak_into_each_other(self, archive):
        """Message ids of different chats overlap (a channel numbers its own); each count uses its own chat's rows and position."""
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OTHER, "a"), (2, OTHER, "b"), (3, OTHER, "c")])
        seed_chat(archive.conn, chat_id=-1000000000001, name="News", chat_type="channel", messages=[(2, None, "x"), (3, None, "y"), (4, None, "z")])
        q.record_read_inbox(archive.conn, 42, 1)
        q.record_read_inbox(archive.conn, -1000000000001, 3)
        counts = {c["chat_id"]: c["unread_count"] for c in rq.get_chats(archive.conn)["items"]}
        assert counts == {42: 2, -1000000000001: 1}

    def test_the_count_is_capped(self, chat, monkeypatch):
        monkeypatch.setattr(rq, "UNREAD_COUNT_CAP", 3)
        q.record_read_inbox(chat, 42, 0)
        assert unread(chat) == 3  # four are unread; the badge says "3 or more"

    def test_the_chat_list_and_a_single_chat_agree(self, chat):
        q.record_read_inbox(chat, 42, 3)
        assert rq.get_chats(chat)["items"][0]["unread_count"] == unread(chat) == 2


class TestCost:
    def test_the_list_counts_in_one_statement_for_the_whole_page_not_one_per_chat(self, archive):
        q.record_archive_owner(archive.conn, OWNER)
        for chat_id in range(1, 8):
            seed_chat(archive.conn, chat_id=chat_id, name=f"c{chat_id}", messages=[(1, OTHER, "a"), (2, OTHER, "b")])
            q.record_read_inbox(archive.conn, chat_id, 1)
        with recorded_sql(archive.conn) as statements:
            items = rq.get_chats(archive.conn)["items"]
        assert [c["unread_count"] for c in items] == [1] * 7
        assert sum("tg_message_id >" in s for s in statements) == 1

    def test_nothing_is_counted_when_no_chat_has_a_position(self, archive):
        q.record_archive_owner(archive.conn, OWNER)
        seed_chat(archive.conn, chat_id=1, name="c", messages=[(1, OTHER, "a")])
        with recorded_sql(archive.conn) as statements:
            assert rq.get_chats(archive.conn)["items"][0]["unread_count"] is None
        assert not any("tg_message_id >" in s for s in statements)

    def test_an_empty_archive_is_fine(self, archive):
        assert rq.get_chats(archive.conn)["items"] == []


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def unmigrated(server_ready):
    """A real archive without the read-position column (what every archive before the unread bar looks like)."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_103).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("ALTER TABLE chats DROP COLUMN read_inbox_max_id"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


def test_an_archive_without_the_read_position_reports_unknown_and_keeps_working(unmigrated):
    q.record_archive_owner(unmigrated, OWNER)
    seed_chat(unmigrated, chat_id=42, name="Bob", messages=[(1, OTHER, "a")])
    assert rq.get_chat(unmigrated, 42)["unread_count"] is None  # type: ignore[index]
    assert [c["unread_count"] for c in rq.get_chats(unmigrated)["items"]] == [None]
    assert scalar(unmigrated, "SELECT 1") == 1
