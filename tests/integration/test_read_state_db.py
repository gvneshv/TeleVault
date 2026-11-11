"""The owner's Telegram reading position at the database level: the migration, the writers the archiver uses, the readers the API uses,
and an archive that has not been migrated yet."""

import contextlib
import logging

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

REVISION = "c5e9a1d3f742"
PREVIOUS_REVISION = "b94e2d7a1c63"


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def position(conn, chat_id):
    return scalar(conn, "SELECT read_inbox_max_id FROM chats WHERE chat_id = :c", c=chat_id)


@contextlib.contextmanager
def recorded_sql(conn):
    """The SQL statements sent through `conn`'s engine while the block runs (the listener is removed again: the archive engine is shared)."""
    statements: list[str] = []

    def listener(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    event.listen(conn.engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(conn.engine, "before_cursor_execute", listener)


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_this_revision_follows_the_previous_one_in_a_single_chain(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_the_column_is_a_nullable_bigint_with_no_default(self, archive):
        row = archive.conn.execute(
            sa.text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'chats' AND column_name = 'read_inbox_max_id'"
            )
        ).one()
        assert tuple(row) == ("bigint", "YES", None)  # NULL = "not known yet", never 0

    def test_a_new_chat_starts_unknown(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        assert position(archive.conn, 42) is None

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        import os

        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_100).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
            with eng.connect() as conn:
                assert q.chats_have_read_state(conn) is False
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert q.chats_have_read_state(conn) is True
            eng.dispose()
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]


# ---------------------------------------------------------------------------
# record_read_inbox / record_read_inboxes
# ---------------------------------------------------------------------------


class TestRecording:
    def test_a_position_is_stored(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        assert q.record_read_inbox(archive.conn, 42, 17) is True
        assert position(archive.conn, 42) == 17

    def test_it_only_moves_forward(self, archive):
        """A late or stale report must never pull the bar back up."""
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        q.record_read_inbox(archive.conn, 42, 20)
        q.record_read_inbox(archive.conn, 42, 12)
        assert position(archive.conn, 42) == 20
        q.record_read_inbox(archive.conn, 42, 25)
        assert position(archive.conn, 42) == 25

    def test_zero_is_a_real_position_and_not_the_same_as_unknown(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        q.record_read_inbox(archive.conn, 42, 0)
        assert position(archive.conn, 42) == 0
        q.record_read_inbox(archive.conn, 42, 3)
        assert position(archive.conn, 42) == 3

    def test_a_chat_the_archive_does_not_hold_is_not_created(self, archive):
        """Otherwise a blocked or never-archived chat would show up in the list just because the owner read it."""
        q.record_read_inbox(archive.conn, 999, 50)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chats") == 0

    def test_an_unchanged_position_writes_nothing(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        q.record_read_inbox(archive.conn, 42, 20)
        xmin = scalar(archive.conn, "SELECT xmin::text FROM chats WHERE chat_id = 42")
        q.record_read_inbox(archive.conn, 42, 20)
        q.record_read_inbox(archive.conn, 42, 5)
        assert scalar(archive.conn, "SELECT xmin::text FROM chats WHERE chat_id = 42") == xmin  # the row version did not change

    def test_many_chats_in_one_call(self, archive):
        for chat_id in (1, 2, 3):
            seed_chat(archive.conn, chat_id=chat_id, name=f"c{chat_id}", messages=[(1, 5, "hi")])
        assert q.record_read_inboxes(archive.conn, [(1, 10), (2, 20), (3, 30), (404, 40)]) is True
        assert [position(archive.conn, c) for c in (1, 2, 3)] == [10, 20, 30]

    def test_an_empty_batch_touches_nothing(self, archive):
        with recorded_sql(archive.conn) as statements:
            assert q.record_read_inboxes(archive.conn, []) is True
        assert statements == []

    def test_a_failed_statement_leaves_the_connection_usable(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        with pytest.raises(Exception):
            q.record_read_inbox(archive.conn, 42, 2**70)  # outside BIGINT
        assert scalar(archive.conn, "SELECT 1") == 1

    def test_a_report_under_the_old_id_of_an_upgraded_group_is_not_written_onto_the_supergroup(self, archive):
        """The old group counted message ids in another numbering; writing its position onto the new chat would put the bar in the wrong place."""
        old, new = -777, -1000000000555
        seed_chat(archive.conn, chat_id=old, name="Book club", chat_type="group", messages=[(1, 5, "hello")])
        q.upsert_chat(archive.conn, chat_id=new, name="Book club", chat_type="supergroup")
        q.record_chat_migration(archive.conn, old, new)
        q.record_read_inbox(archive.conn, old, 9000)
        assert position(archive.conn, new) is None


# ---------------------------------------------------------------------------
# What the API reads
# ---------------------------------------------------------------------------


class TestReading:
    def test_the_chat_and_the_chat_list_carry_the_position(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        seed_chat(archive.conn, chat_id=43, name="Work", messages=[(1, 5, "hi")])
        q.record_read_inbox(archive.conn, 42, 17)
        assert rq.get_chat(archive.conn, 42)["read_inbox_max_id"] == 17  # type: ignore[index]
        assert rq.get_chat(archive.conn, 43)["read_inbox_max_id"] is None  # type: ignore[index]
        listed = {c["chat_id"]: c["read_inbox_max_id"] for c in rq.get_chats(archive.conn)["items"]}
        assert listed == {42: 17, 43: None}


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def unmigrated(server_ready):
    """A real archive with the column dropped again - what every archive created before this feature looks like."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_101).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("ALTER TABLE chats DROP COLUMN read_inbox_max_id"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestArchiveWithoutTheColumn:
    """Nothing migrates an existing archive automatically, so the archiver and the chat list must keep working exactly as before until it is migrated."""

    def test_recording_is_skipped_quietly_and_the_connection_stays_usable(self, unmigrated, monkeypatch):
        monkeypatch.setattr(q, "_read_state_missing_warned", False)
        seed_chat(unmigrated, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        with recorded_sql(unmigrated) as statements:
            assert q.record_read_inbox(unmigrated, 42, 17) is False
        assert not any("UPDATE chats" in s for s in statements)  # a failing statement would be an ERROR line in Postgres for every read event
        assert scalar(unmigrated, "SELECT 1") == 1

    def test_it_warns_once_not_per_event(self, unmigrated, monkeypatch, caplog):
        monkeypatch.setattr(q, "_read_state_missing_warned", False)
        with caplog.at_level(logging.WARNING, logger="db.queries"):
            for _ in range(5):
                q.record_read_inbox(unmigrated, 42, 17)
        warnings = [r for r in caplog.records if "read-position column" in r.getMessage()]
        assert len(warnings) == 1
        assert "migrate_archives.py" in warnings[0].getMessage()

    def test_the_chat_list_still_works_and_reports_unknown(self, unmigrated):
        seed_chat(unmigrated, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        assert rq.get_chat(unmigrated, 42)["read_inbox_max_id"] is None  # type: ignore[index]
        assert [c["read_inbox_max_id"] for c in rq.get_chats(unmigrated)["items"]] == [None]

    def test_an_archive_migrated_while_running_starts_recording_without_a_restart(self, unmigrated):
        seed_chat(unmigrated, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        assert q.record_read_inbox(unmigrated, 42, 17) is False
        unmigrated.execute(sa.text("ALTER TABLE chats ADD COLUMN read_inbox_max_id BIGINT"))
        unmigrated.commit()
        assert q.record_read_inbox(unmigrated, 42, 17) is True
        assert position(unmigrated, 42) == 17

    def test_once_present_it_is_not_probed_again(self, archive):
        seed_chat(archive.conn, chat_id=42, name="Family", messages=[(1, 5, "hi")])
        q.chats_have_read_state(archive.conn)  # primes the per-process cache
        with recorded_sql(archive.conn) as statements:
            for _ in range(3):
                q.record_read_inbox(archive.conn, 42, 17)
        assert not any("pg_attribute" in s for s in statements)
