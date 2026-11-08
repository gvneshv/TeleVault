"""
Sender name history and display-name overrides at the database level:
the migration, upsert_sender()'s change tracking (and its promise never to cost a message), the read queries, and what a chat purge does to both tables.
"""

import logging
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.pool import NullPool

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT
from tests.helpers import T0, seed_chat

REVISION = "a3f81c5d7e92"
PREVIOUS_REVISION = "7c1d4a9e2b35"


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def history(conn, sender_id):
    """The recorded changes of one sender, oldest first, as (username, first_name, last_name)."""
    rows = conn.execute(
        sa.text("SELECT username, first_name, last_name FROM sender_name_history WHERE sender_id = :s ORDER BY id"), {"s": sender_id}
    )
    return [tuple(r) for r in rows]


def see(conn, sender_id=7, username="olga", first_name="Olga", last_name="K", **kw):
    q.upsert_sender(conn, sender_id=sender_id, username=username, first_name=first_name, last_name=last_name, **kw)


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_the_migration_follows_the_previous_head_and_the_chain_has_one_head(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_a_provisioned_archive_has_both_tables(self, archive):
        cols = lambda t: {  # noqa: E731
            r[0]: r[1]
            for r in archive.conn.execute(
                sa.text("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t"), {"t": t}
            )
        }
        assert cols("sender_name_history") == {
            "id": "NO", "sender_id": "NO", "username": "YES", "first_name": "YES", "last_name": "YES", "seen_at": "NO"
        }
        assert cols("sender_display_names") == {"sender_id": "NO", "display_name": "NO", "updated_at": "NO"}

    def test_the_models_and_the_migration_agree(self, archive):
        """Alembic's own drift check: the migration chain must produce exactly what db/schema.py declares for the two new tables."""
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        from db.schema import metadata

        ctx = MigrationContext.configure(archive.conn)
        diffs = [d for d in compare_metadata(ctx, metadata) if "sender_name_history" in repr(d) or "sender_display_names" in repr(d)]
        assert diffs == []

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """Reverses cleanly on a throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_080).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.sender_name_history')") is None
                assert scalar(conn, "SELECT to_regclass('public.sender_display_names')") is None
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.sender_name_history')") is not None
                assert scalar(conn, "SELECT to_regclass('public.sender_display_names')") is not None
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()


# ---------------------------------------------------------------------------
# upsert_sender: what counts as a name change
# ---------------------------------------------------------------------------


class TestNameChangeTracking:
    def test_a_new_sender_has_no_history_its_row_is_the_first_identity(self, archive):
        see(archive.conn)
        assert history(archive.conn, 7) == []

    def test_seeing_the_same_name_again_records_nothing(self, archive):
        see(archive.conn)
        see(archive.conn)
        assert history(archive.conn, 7) == []

    @pytest.mark.parametrize(
        "change, expected",
        [
            ({"username": "olga_new"}, ("olga_new", "Olga", "K")),
            ({"first_name": "Olha"}, ("olga", "Olha", "K")),
            ({"last_name": "Kovalenko"}, ("olga", "Olga", "Kovalenko")),
        ],
    )
    def test_a_change_to_any_one_field_is_recorded_as_a_full_snapshot(self, archive, change, expected):
        see(archive.conn)
        see(archive.conn, **change)
        assert history(archive.conn, 7) == [expected]

    def test_the_senders_row_keeps_the_first_seen_identity(self, archive):
        see(archive.conn)
        see(archive.conn, username="olga_new", first_name="Olha")
        row = archive.conn.execute(sa.text("SELECT username, first_name, last_name FROM senders WHERE sender_id = 7")).one()
        assert tuple(row) == ("olga", "Olga", "K")

    def test_a_missing_field_means_not_provided_and_is_never_a_change(self, archive):
        """Telegram sends partial ('min') users without a username; that must not look like the username being removed."""
        see(archive.conn)
        see(archive.conn, username=None)
        see(archive.conn, username=None, first_name=None, last_name=None)
        assert history(archive.conn, 7) == []

    def test_a_missing_field_keeps_the_last_known_value_in_the_next_snapshot(self, archive):
        see(archive.conn)
        see(archive.conn, username=None, first_name="Olha")
        assert history(archive.conn, 7) == [("olga", "Olha", "K")]

    def test_a_username_appearing_for_the_first_time_is_a_change(self, archive):
        see(archive.conn, username=None)
        see(archive.conn, username="olga")
        assert history(archive.conn, 7) == [("olga", "Olga", "K")]

    def test_changing_back_to_an_earlier_name_is_a_new_entry(self, archive):
        """The comparison is against the LATEST known identity, not the first-seen row - otherwise A -> B -> A would record only B."""
        see(archive.conn, first_name="Olga")
        see(archive.conn, first_name="Olha")
        see(archive.conn, first_name="Olga")
        assert [h[1] for h in history(archive.conn, 7)] == ["Olha", "Olga"]

    def test_the_same_new_name_seen_twice_is_one_entry(self, archive):
        see(archive.conn)
        see(archive.conn, first_name="Olha")
        see(archive.conn, first_name="Olha")
        assert len(history(archive.conn, 7)) == 1

    def test_senders_are_tracked_separately(self, archive):
        see(archive.conn, sender_id=7)
        see(archive.conn, sender_id=8, first_name="Ivan")
        see(archive.conn, sender_id=7, first_name="Olha")
        assert len(history(archive.conn, 7)) == 1 and history(archive.conn, 8) == []

    def test_with_commit_false_the_history_is_part_of_the_callers_transaction(self, archive):
        """The workers upsert chat + sender + message in one transaction: a rollback must take the history row with it."""
        see(archive.conn)
        see(archive.conn, first_name="Olha", commit=False)
        archive.conn.rollback()
        assert history(archive.conn, 7) == []
        see(archive.conn, first_name="Olha", commit=False)
        archive.conn.commit()
        assert len(history(archive.conn, 7)) == 1


class TestNameHistoryNeverCostsAMessage:
    def test_a_failing_history_write_is_undone_alone_and_the_message_is_still_archived(self, archive, monkeypatch, caplog):
        """The write runs in a SAVEPOINT: when it fails, the caller's transaction (here chat + sender + message) must survive."""
        see(archive.conn)
        monkeypatch.setattr(q, "_RECORD_NAME_CHANGE_SQL", "INSERT INTO table_that_does_not_exist VALUES (1)")
        q.upsert_chat(archive.conn, chat_id=1, name="c", chat_type="private", username=None, commit=False)
        with caplog.at_level(logging.WARNING, logger=q.logger.name):
            see(archive.conn, first_name="Olha", commit=False)
        q.insert_message(archive.conn, tg_message_id=1, chat_id=1, sender_id=7, text="still here", date=T0, commit=False)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages") == 1
        assert "Could not record a name change for sender 7" in caplog.text

    def test_an_archive_without_the_table_is_not_an_error_and_logs_nothing_at_the_database(self, server_ready, caplog):
        """Un-migrated archives (nothing migrates them automatically) must keep archiving exactly as before."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_081).db_name
        eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
        try:
            with eng.begin() as conn:
                conn.execute(sa.text("DROP TABLE sender_display_names"))
                conn.execute(sa.text("DROP TABLE sender_name_history"))
            with eng.connect() as conn:
                see(conn)
                with caplog.at_level(logging.WARNING, logger=q.logger.name):
                    see(conn, first_name="Olha")
                    see(conn, first_name="Olha", commit=False)
                    conn.rollback()
                assert scalar(conn, "SELECT COUNT(*) FROM senders") == 1
                assert [r for r in caplog.records if r.name == q.logger.name] == []  # not even a warning: the probe answers quietly instead of failing a statement
        finally:
            eng.dispose()

    def test_an_archive_migrated_while_the_archiver_runs_starts_recording_without_a_restart(self, server_ready):
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_082).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                see(conn)
                see(conn, first_name="Olha")  # no table yet: silently nothing
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                see(conn, first_name="Olga2")
                assert len(history(conn, 7)) == 1
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------


class TestDisplayNameQueries:
    def test_set_replace_and_clear(self, archive):
        see(archive.conn)
        first = q.set_sender_display_name(archive.conn, 7, "Mum")
        assert first["sender_id"] == 7 and first["display_name"] == "Mum"
        second = q.set_sender_display_name(archive.conn, 7, "Mom")
        assert second["display_name"] == "Mom" and second["updated_at"] >= first["updated_at"]
        assert rq.get_sender_display_names(archive.conn) == [{"sender_id": 7, "display_name": "Mom"}]
        assert q.clear_sender_display_name(archive.conn, 7) is True
        assert q.clear_sender_display_name(archive.conn, 7) is False  # idempotent
        assert rq.get_sender_display_names(archive.conn) == []

    def test_a_label_for_an_unknown_sender_is_refused_by_the_database(self, archive):
        with pytest.raises(IntegrityError):
            q.set_sender_display_name(archive.conn, 12345, "Ghost")
        assert scalar(archive.conn, "SELECT 1") == 1  # the failed write was rolled back: the connection is still usable

    def test_labels_are_listed_by_sender_id(self, archive):
        for sid in (30, 10, 20):
            see(archive.conn, sender_id=sid)
            q.set_sender_display_name(archive.conn, sid, f"n{sid}")
        assert [r["sender_id"] for r in rq.get_sender_display_names(archive.conn)] == [10, 20, 30]

    def test_the_label_does_not_change_the_message_queries(self, archive):
        """Labels are applied by the UI; the message endpoints keep returning the sender as Telegram named them."""
        seed_chat(archive.conn, 1, "c", messages=[(1, 7, "hello")])
        q.set_sender_display_name(archive.conn, 7, "Mum")
        sender = rq.get_messages(archive.conn)["items"][0]["sender"]
        assert sender["display_name"] is None and sender["first_name"] == "User7"


class TestSenderDetail:
    def test_an_unknown_sender_is_none(self, archive):
        assert rq.get_sender_detail(archive.conn, 404) is None

    def test_a_sender_who_never_changed_has_one_history_entry_the_first_identity(self, archive):
        see(archive.conn)
        d = rq.get_sender_detail(archive.conn, 7)
        assert (d["username"], d["first_name"], d["last_name"]) == ("olga", "Olga", "K")
        assert d["display_name"] is None and d["display_name_updated_at"] is None
        assert [(h["username"], h["first_name"]) for h in d["history"]] == [("olga", "Olga")]

    def test_history_is_newest_first_and_ends_with_the_first_identity(self, archive):
        see(archive.conn)
        see(archive.conn, first_name="Olha")
        see(archive.conn, first_name="Olya", username="olya")
        d = rq.get_sender_detail(archive.conn, 7)
        assert [(h["username"], h["first_name"]) for h in d["history"]] == [("olya", "Olya"), ("olga", "Olha"), ("olga", "Olga")]
        # the plain fields are still the FIRST identity, history[0] is the latest
        assert d["first_name"] == "Olga"

    def test_the_label_is_included(self, archive):
        see(archive.conn)
        q.set_sender_display_name(archive.conn, 7, "Mum")
        d = rq.get_sender_detail(archive.conn, 7)
        assert d["display_name"] == "Mum" and d["display_name_updated_at"] is not None

    def test_history_is_capped_keeping_the_newest(self, archive, monkeypatch):
        monkeypatch.setattr(rq, "SENDER_HISTORY_MAX_ENTRIES", 3)
        see(archive.conn, first_name="n0")
        for i in range(1, 6):
            see(archive.conn, first_name=f"n{i}")
        d = rq.get_sender_detail(archive.conn, 7)
        assert [h["first_name"] for h in d["history"]] == ["n5", "n4", "n3"]

    def test_an_archive_without_the_tables_still_answers_reads(self, server_ready):
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_083).db_name
        eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
        try:
            with eng.begin() as conn:
                conn.execute(sa.text("DROP TABLE sender_display_names"))
                conn.execute(sa.text("DROP TABLE sender_name_history"))
            with eng.connect() as conn:
                see(conn)
                assert rq.get_sender_display_names(conn) == []
                d = rq.get_sender_detail(conn, 7)
                assert d is not None and d["display_name"] is None and len(d["history"]) == 1
                assert scalar(conn, "SELECT 1") == 1  # the failed reads did not leave the transaction aborted
                with pytest.raises(ProgrammingError):  # writes are not softened: the API turns this into a 409
                    q.set_sender_display_name(conn, 7, "Mum")
        finally:
            eng.dispose()


# ---------------------------------------------------------------------------
# Purge: what was recorded about a person goes with them
# ---------------------------------------------------------------------------


class TestPurge:
    def test_purging_a_chat_removes_the_names_of_senders_left_without_messages(self, archive):
        seed_chat(archive.conn, 1, "gone", messages=[(1, 7, "hi")])
        seed_chat(archive.conn, 2, "stays", messages=[(1, 8, "yo")])
        for sid in (7, 8):  # seed_chat already created both senders, so one sighting under a new name is exactly one history row
            see(archive.conn, sender_id=sid, first_name="Changed")
            q.set_sender_display_name(archive.conn, sid, f"label{sid}")
        assert q.purge_chat(archive.conn, 1) is not None
        assert scalar(archive.conn, "SELECT COUNT(*) FROM sender_name_history WHERE sender_id = 7") == 0
        assert scalar(archive.conn, "SELECT COUNT(*) FROM sender_display_names WHERE sender_id = 7") == 0
        # a sender who still has messages elsewhere keeps both
        assert scalar(archive.conn, "SELECT COUNT(*) FROM sender_name_history WHERE sender_id = 8") == 1
        assert scalar(archive.conn, "SELECT COUNT(*) FROM sender_display_names WHERE sender_id = 8") == 1
