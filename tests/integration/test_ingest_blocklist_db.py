"""The ingest blocklist at the database level: the migration, the queries the workers and the API share, and how a rule survives a group upgrade."""

import logging

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import ProgrammingError

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT
from tests.helpers import seed_chat

REVISION = "0e528a688c4c"
PREVIOUS_REVISION = "dd006ba3b06d"


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def rules(conn):
    return {r["chat_id"]: r["name"] for r in rq.get_blocked_chats(conn)}


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_the_migration_chain_has_exactly_one_head_and_it_is_this_revision(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        assert ScriptDirectory.from_config(cfg).get_heads() == [REVISION]

    def test_a_provisioned_archive_has_the_table(self, archive):
        columns = {
            r[0]: r[1]
            for r in archive.conn.execute(
                sa.text("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'ingest_blocked_chats'")
            )
        }
        assert columns == {"chat_id": "NO", "name": "YES", "blocked_at": "NO"}

    def test_a_rule_has_no_foreign_key_to_chats(self, archive):
        """Deleting a chat's data (a planned feature) must never take its block with it."""
        fks = scalar(
            archive.conn,
            "SELECT COUNT(*) FROM information_schema.table_constraints "
            "WHERE table_name = 'ingest_blocked_chats' AND constraint_type = 'FOREIGN KEY'",
        )
        assert fks == 0

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """The migration reverses cleanly, on its own throwaway archive (the shared one must stay at head)."""
        import os

        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_010).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.ingest_blocked_chats')") is None
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.ingest_blocked_chats')") is not None
            eng.dispose()
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]


# ---------------------------------------------------------------------------
# block_chat / unblock_chat / is_chat_blocked
# ---------------------------------------------------------------------------


class TestBlocklistQueries:
    def test_nothing_is_blocked_by_default(self, archive):
        assert q.is_chat_blocked(archive.conn, 42) is False
        assert rules(archive.conn) == {}

    def test_block_then_unblock(self, archive):
        assert q.block_chat(archive.conn, 42, "Family") is True
        assert q.is_chat_blocked(archive.conn, 42) is True
        assert q.is_chat_blocked(archive.conn, 43) is False  # only that chat
        assert q.unblock_chat(archive.conn, 42) is True
        assert q.is_chat_blocked(archive.conn, 42) is False

    def test_blocking_twice_keeps_the_first_rule(self, archive):
        assert q.block_chat(archive.conn, 42, "Family") is True
        assert q.block_chat(archive.conn, 42, "Renamed") is False
        assert rules(archive.conn) == {42: "Family"}

    def test_unblocking_an_unblocked_chat_reports_nothing_removed(self, archive):
        assert q.unblock_chat(archive.conn, 42) is False

    def test_a_rule_outlives_the_chat_it_names(self, archive):
        """The planned 'delete everything stored for this chat' must not silently lift the block."""
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hello")])
        q.block_chat(archive.conn, 42, "Family")
        archive.conn.execute(sa.text("DELETE FROM messages"))
        archive.conn.execute(sa.text("DELETE FROM chats"))
        archive.conn.commit()
        assert q.is_chat_blocked(archive.conn, 42) is True
        assert rules(archive.conn) == {42: "Family"}

    def test_a_failed_statement_leaves_the_connection_usable(self, archive):
        with pytest.raises(Exception):
            q.block_chat(archive.conn, 2**70, "overflow")  # outside BIGINT
        assert scalar(archive.conn, "SELECT 1") == 1

    def test_blocked_chats_list_newest_first(self, archive):
        q.block_chat(archive.conn, 1, "First")
        archive.conn.execute(sa.text("UPDATE ingest_blocked_chats SET blocked_at = blocked_at - interval '1 day'"))
        archive.conn.commit()
        q.block_chat(archive.conn, 2, "Second")
        assert [r["chat_id"] for r in rq.get_blocked_chats(archive.conn)] == [2, 1]


# ---------------------------------------------------------------------------
# Group -> supergroup upgrades
# ---------------------------------------------------------------------------


class TestChatMigration:
    OLD, NEW = -777, -1000000000555

    def test_a_blocked_group_stays_blocked_after_it_is_upgraded(self, archive):
        """Regression guard for the main way a blocklist silently stops working: the chat gets a new id."""
        q.block_chat(archive.conn, self.OLD, "Book club")
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert q.is_chat_blocked(archive.conn, self.NEW) is True

    def test_the_rule_is_moved_not_duplicated(self, archive):
        q.block_chat(archive.conn, self.OLD, "Book club")
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert rules(archive.conn) == {self.NEW: "Book club"}

    def test_a_late_message_under_the_old_id_is_still_blocked(self, archive):
        q.block_chat(archive.conn, self.OLD, "Book club")
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert q.is_chat_blocked(archive.conn, self.OLD) is True

    def test_blocking_the_old_id_after_the_upgrade_blocks_the_canonical_chat(self, archive):
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert q.block_chat(archive.conn, self.OLD, "Book club") is True
        assert rules(archive.conn) == {self.NEW: "Book club"}

    def test_an_existing_rule_on_the_new_id_wins(self, archive):
        q.block_chat(archive.conn, self.OLD, "Old name")
        q.block_chat(archive.conn, self.NEW, "New name")
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert rules(archive.conn) == {self.NEW: "New name"}

    def test_an_unblocked_group_stays_unblocked_after_the_upgrade(self, archive):
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert q.is_chat_blocked(archive.conn, self.NEW) is False
        assert rules(archive.conn) == {}

    def test_recording_the_same_upgrade_twice_changes_nothing(self, archive):
        q.block_chat(archive.conn, self.OLD, "Book club")
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert rules(archive.conn) == {self.NEW: "Book club"}


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def unmigrated(server_ready):
    """A real archive with the blocklist table dropped again - what every archive created before this feature looks like."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_011).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE ingest_blocked_chats"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestArchiveWithoutTheTable:
    """Nothing migrates an existing archive automatically, so the workers must keep archiving exactly as before until it is migrated."""

    def test_nothing_is_blocked_and_the_connection_stays_usable(self, unmigrated, monkeypatch):
        monkeypatch.setattr(q, "_blocklist_table_missing_warned", False)
        assert q.is_chat_blocked(unmigrated, 42) is False
        assert scalar(unmigrated, "SELECT 1") == 1  # the aborted transaction was rolled back, not left poisoning the connection

    def test_it_warns_once_not_per_message(self, unmigrated, monkeypatch, caplog):
        monkeypatch.setattr(q, "_blocklist_table_missing_warned", False)
        with caplog.at_level(logging.WARNING, logger="db.queries"):
            for _ in range(5):
                q.is_chat_blocked(unmigrated, 42)
        warnings = [r for r in caplog.records if "ingest_blocked_chats" in r.getMessage()]
        assert len(warnings) == 1
        assert "migrate_archives.py" in warnings[0].getMessage()  # the fix we point at must be the one that works

    def test_a_group_upgrade_is_still_recorded(self, unmigrated):
        seed_chat(unmigrated, chat_id=-777, name="Book club", chat_type="group", messages=[(1, 5, "hello")])
        q.upsert_chat(unmigrated, chat_id=-1000000000555, name="Book club", chat_type="supergroup")
        q.record_chat_migration(unmigrated, -777, -1000000000555)
        assert scalar(unmigrated, "SELECT COUNT(*) FROM chat_migrations") == 1
        assert scalar(unmigrated, "SELECT COUNT(*) FROM messages WHERE chat_id = -1000000000555") == 1  # and the merge still ran

    def test_only_a_missing_table_means_no_rules(self, unmigrated):
        """
        A table that exists but is wrong (here: a missing column - also a ProgrammingError, but SQLSTATE 42703, not 42P01) must surface,
        so the caller skips the message instead of guessing 'not blocked' and archiving a chat the person may have blocked.
        """
        unmigrated.execute(sa.text("CREATE TABLE ingest_blocked_chats (cid BIGINT)"))
        unmigrated.commit()
        with pytest.raises(ProgrammingError):
            q.is_chat_blocked(unmigrated, 42)
        assert scalar(unmigrated, "SELECT 1") == 1  # still rolled back
