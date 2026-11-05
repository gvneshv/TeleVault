"""The recorded owner of an archive at the database level: the migration and the two queries the archiver (write) and the API (read) share."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT

REVISION = "7c1d4a9e2b35"
PREVIOUS_REVISION = "0e528a688c4c"


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


class TestSchema:
    def test_a_provisioned_archive_has_the_table(self, archive):
        columns = {
            r[0]: r[1]
            for r in archive.conn.execute(
                sa.text("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'archive_owner'")
            )
        }
        assert columns == {"singleton": "NO", "owner_id": "NO", "recorded_at": "NO"}

    def test_a_second_row_is_impossible(self, archive):
        """The table holds exactly one owner; the database itself refuses anything else, not just the application."""
        q.record_archive_owner(archive.conn, 111)
        with pytest.raises(IntegrityError):
            archive.conn.execute(sa.text("INSERT INTO archive_owner (singleton, owner_id) VALUES (FALSE, 222)"))
        archive.conn.rollback()
        with pytest.raises(IntegrityError):
            archive.conn.execute(sa.text("INSERT INTO archive_owner (singleton, owner_id) VALUES (TRUE, 222)"))
        archive.conn.rollback()
        assert scalar(archive.conn, "SELECT COUNT(*) FROM archive_owner") == 1

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """The migration reverses cleanly, on its own throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_011).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.archive_owner')") is None
                assert rq.get_archive_owner(conn) is None  # an archive that predates the migration: unknown, not an error
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.archive_owner')") is not None
            eng.dispose()
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]


class TestQueries:
    def test_the_owner_is_unknown_until_recorded(self, archive):
        assert rq.get_archive_owner(archive.conn) is None

    def test_a_recorded_owner_is_read_back(self, archive):
        q.record_archive_owner(archive.conn, 5_000_000_001)  # beyond 32 bits, like real Telegram ids
        assert rq.get_archive_owner(archive.conn) == 5_000_000_001

    def test_recording_again_replaces_the_owner(self, archive):
        """The account was re-linked to someone else: the latest archiver wins, and there is still just one row."""
        q.record_archive_owner(archive.conn, 1)
        q.record_archive_owner(archive.conn, 2)
        assert rq.get_archive_owner(archive.conn) == 2
        assert scalar(archive.conn, "SELECT COUNT(*) FROM archive_owner") == 1

    def test_recording_the_same_owner_twice_is_harmless(self, archive):
        q.record_archive_owner(archive.conn, 7)
        q.record_archive_owner(archive.conn, 7)
        assert rq.get_archive_owner(archive.conn) == 7

    def test_a_missing_table_reads_as_unknown_and_leaves_the_connection_usable(self, archive):
        """Postgres aborts a transaction after a failed statement - the read must clean up after itself."""
        archive.conn.execute(sa.text("ALTER TABLE archive_owner RENAME TO archive_owner_away"))
        archive.conn.commit()
        try:
            assert rq.get_archive_owner(archive.conn) is None
            assert scalar(archive.conn, "SELECT 1") == 1  # not "current transaction is aborted"
        finally:
            archive.conn.execute(sa.text("ALTER TABLE archive_owner_away RENAME TO archive_owner"))
            archive.conn.commit()


class TestArchiverStartup:
    def test_the_archiver_helper_writes_what_the_api_reads(self, archive):
        """main._record_owner is the worker's half: it must land in the same table get_archive_owner() reads."""
        import main

        db.init_db(db.server_database_url(archive.name).render_as_string(hide_password=False))
        try:
            main._record_owner(424242)
        finally:
            db.close_db()
        assert rq.get_archive_owner(archive.conn) == 424242

    def test_start_up_records_the_owner_but_never_fails_because_of_it(self):
        """
        Running main() needs a real Telegram login, so this pins the wiring by reading the source:
        the id Telegram reported (`me.id`) is recorded after the handlers are registered,
        and a failure is caught - an archive that is not migrated yet must not stop archiving.
        """
        import inspect

        import main

        source = inspect.getsource(main.main)
        call = source.index("asyncio.to_thread(_record_owner, me.id)")
        assert source.index("register_handlers(client, self_id=me.id)") < call
        guard = source[:call].rindex("try:")
        assert "except Exception" in source[call : call + 400] and guard < call
