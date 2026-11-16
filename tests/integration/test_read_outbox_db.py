"""
Read receipts at the database level: how far the OTHER person has read the owner's messages (chats.read_outbox_max_id).

What these tests pin:
it is stored for PRIVATE chats only (the owner decided against groups: flood limits, legal exposure) and that rule holds in the write path AND the read path;
it only moves forward; it never creates a chat; it is independent of the owner's own reading position;
and an archive without the column keeps working.
"""

import logging
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

REVISION = "f1c6d8a39b27"
PREVIOUS_REVISION = "e4a8c2b6d913"
OWNER = 1
OTHER = 50


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def stored(conn, chat_id):
    """The raw stored number, bypassing every read-side guard."""
    return scalar(conn, "SELECT read_outbox_max_id FROM chats WHERE chat_id = :c", c=chat_id)


def served(conn, chat_id):
    """What the API would serve for the chat."""
    chat = rq.get_chat(conn, chat_id)
    assert chat is not None
    return chat["read_outbox_max_id"]


@pytest.fixture
def private(archive):
    q.record_archive_owner(archive.conn, OWNER)
    seed_chat(archive.conn, chat_id=42, name="Bob", messages=[(1, OWNER, "hi"), (2, OTHER, "hello"), (3, OWNER, "how are you")])
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

    def test_a_provisioned_archive_has_the_column_and_it_starts_empty(self, private):
        assert q.chats_have_read_outbox(private) is True
        assert stored(private, 42) is None

    def test_the_declared_model_has_the_column(self):
        from db.schema import chats

        assert "read_outbox_max_id" in chats.c
        assert isinstance(chats.c.read_outbox_max_id.type, sa.BigInteger)

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_106).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)

        def has_column(conn):
            return bool(
                scalar(
                    conn,
                    "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('public.chats') "
                    "AND attname = 'read_outbox_max_id' AND NOT attisdropped)",
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
# Recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_a_private_chats_position_is_stored_and_served(self, private):
        assert q.record_read_outbox(private, 42, 1) is True
        assert stored(private, 42) == 1
        assert served(private, 42) == 1

    def test_it_only_moves_forward(self, private):
        q.record_read_outbox(private, 42, 3)
        q.record_read_outbox(private, 42, 1)  # an older report that arrives late
        assert stored(private, 42) == 3

    def test_zero_means_nothing_read_yet_and_is_a_real_position(self, private):
        q.record_read_outbox(private, 42, 0)
        assert served(private, 42) == 0

    def test_a_batch_is_stored_in_one_go(self, archive):
        for chat_id in (1, 2, 3):
            seed_chat(archive.conn, chat_id=chat_id, name=f"c{chat_id}", messages=[(1, OWNER, "a")])
        q.record_read_outboxes(archive.conn, [(1, 5), (2, 6), (3, 7)])
        assert [stored(archive.conn, c) for c in (1, 2, 3)] == [5, 6, 7]

    @pytest.mark.parametrize("chat_type, chat_id", [("group", -9), ("supergroup", -1000000000077), ("channel", -1000000000078)])
    def test_a_group_or_channel_is_never_stored(self, archive, chat_type, chat_id):
        """Owner's decision: following many people's reading means flood limits and legal exposure.
        Refused in the database layer itself."""
        seed_chat(archive.conn, chat_id=chat_id, name="Team", chat_type=chat_type, messages=[(1, OWNER, "a")])
        q.record_read_outbox(archive.conn, chat_id, 1)
        assert stored(archive.conn, chat_id) is None

    def test_a_group_that_somehow_has_a_number_is_still_not_served(self, archive):
        """The read path is its own guard: even a stored value never reaches the page for a group."""
        seed_chat(archive.conn, chat_id=-9, name="Team", chat_type="group", messages=[(1, OWNER, "a")])
        archive.conn.execute(sa.text("UPDATE chats SET read_outbox_max_id = 1 WHERE chat_id = -9"))
        archive.conn.commit()
        assert served(archive.conn, -9) is None
        assert [c["read_outbox_max_id"] for c in rq.get_chats(archive.conn)["items"]] == [None]

    def test_an_unknown_chat_is_not_created(self, archive):
        """A chat with no row (a blocked one above all) must not appear in the list because of a read receipt."""
        q.record_read_outbox(archive.conn, 777, 5)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chats") == 0

    def test_it_is_independent_of_the_owners_own_reading_position(self, private):
        q.record_read_inbox(private, 42, 2)
        q.record_read_outbox(private, 42, 1)
        chat = rq.get_chat(private, 42)
        assert (chat["read_inbox_max_id"], chat["read_outbox_max_id"]) == (2, 1)  # type: ignore[index]

    def test_the_list_and_a_single_chat_agree(self, private):
        q.record_read_outbox(private, 42, 3)
        assert rq.get_chats(private)["items"][0]["read_outbox_max_id"] == served(private, 42) == 3

    def test_unknown_stays_null_not_zero(self, private):
        assert served(private, 42) is None


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def without_column(server_ready):
    """A real archive that has everything but the read-receipt column (every archive before this change)."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_107).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("ALTER TABLE chats DROP COLUMN read_outbox_max_id"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestUnmigratedArchive:
    def test_the_chat_list_keeps_working_and_reports_unknown(self, without_column):
        seed_chat(without_column, chat_id=42, name="Bob", messages=[(1, OWNER, "a")])
        assert q.chats_have_read_outbox(without_column) is False
        assert served(without_column, 42) is None
        assert [c["read_outbox_max_id"] for c in rq.get_chats(without_column)["items"]] == [None]

    def test_recording_is_skipped_with_one_warning_not_one_per_event(self, without_column, monkeypatch, caplog):
        monkeypatch.setattr(q, "_read_outbox_missing_warned", False)
        seed_chat(without_column, chat_id=42, name="Bob", messages=[(1, OWNER, "a")])
        with caplog.at_level(logging.WARNING, logger="db.queries"):
            assert q.record_read_outbox(without_column, 42, 1) is False
            assert q.record_read_outbox(without_column, 42, 2) is False
        warnings = [r for r in caplog.records if "read-receipt column" in r.getMessage()]
        assert len(warnings) == 1
