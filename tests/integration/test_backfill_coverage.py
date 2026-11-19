"""
backfill.py's incremental mode against a real archive: which part of a chat is already "covered", and what moves that marker.

Regression at the heart of this file: the marker used to be the highest message id stored - which the LIVE archiver raises too -
so a chat first seen live (say at message 1000) was treated as covered up to 1000 and its older history was never fetched.
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
from tests.conftest import REPO_ROOT
from tests.helpers import T0
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (shared fixture and helpers)


def covered(conn, chat_id):
    conn.rollback()  # see what backfill.run() committed from its own connection
    return q.get_backfill_covered_id(conn, chat_id)


def seed_live(conn, chat_id, tg_message_id):
    """What the live archiver does: store a message of a chat, with no backfill involved."""
    q.upsert_chat(conn, chat_id=chat_id, name=f"chat{chat_id}", chat_type="private")
    q.insert_message(conn, tg_message_id=tg_message_id, chat_id=chat_id, sender_id=None, text="live", date=T0)


def test_a_chat_first_seen_live_still_gets_its_older_history(archive, telegram):  # noqa: F811
    seed_live(archive.conn, 11, 1000)
    fake, _ = telegram({11: ["one", "two", "three", "four", "five"]})
    assert fake.iter_calls[0]["min_id"] == 0  # not 1000
    assert stored(archive.conn) == {11: 6}  # the live message and all five from history


def test_the_marker_is_where_the_walk_got_to_and_the_next_run_starts_there(archive, telegram):  # noqa: F811
    telegram({11: ["a", "b", "c", "d", "e"]})
    assert covered(archive.conn, 11) == 5
    fake, _ = telegram({11: ["a", "b", "c", "d", "e"]})
    assert fake.iter_calls[0]["min_id"] == 5


def test_messages_that_were_deliberately_not_stored_still_count_as_covered(archive, telegram):  # noqa: F811
    """Otherwise every run would re-fetch them just to skip them again."""
    telegram({11: [("timed", None, 60), ("timed", None, 60)]})
    assert stored(archive.conn) == {}
    assert covered(archive.conn, 11) == 2


def test_full_ignores_the_marker_but_still_moves_it(archive, telegram):  # noqa: F811
    telegram({11: ["a", "b", "c"]})
    fake, _ = telegram({11: ["a", "b", "c", "d", "e"]}, force_full=True)
    assert fake.iter_calls[0]["min_id"] == 0
    assert covered(archive.conn, 11) == 5


def test_a_cancel_in_the_middle_of_a_chat_keeps_what_was_done(archive, telegram):  # noqa: F811
    def cancel_before_the_fourth(chat_id, message_id):
        if message_id == 4:
            import backfill
            backfill._cancelled = True

    fake, status = telegram({11: ["a", "b", "c", "d", "e"]}, setup=lambda f: setattr(f, "before_message", cancel_before_the_fourth))
    assert status["state"] == "cancelled"
    assert stored(archive.conn) == {11: 3}
    assert covered(archive.conn, 11) == 3  # the next run resumes after message 3, not at the start and not at the end


def test_a_chat_that_failed_is_not_marked_as_covered(archive, telegram, monkeypatch):  # noqa: F811
    def broken(conn, chat_id):
        raise RuntimeError("rules unreadable")

    monkeypatch.setattr(q, "get_ignore_patterns", broken)
    telegram({11: ["a", "b"]})
    assert covered(archive.conn, 11) is None


@pytest.fixture
def archive_without_the_column(archive, monkeypatch):
    """
    The shared archive as it was before migration b6f2a8d1c394.
    The `archive` fixture is one database for the whole test run (it is only truncated between tests),
    so the column is put back afterwards - leaving it dropped would break every later test that backfills.
    """
    archive.conn.execute(sa.text("ALTER TABLE chats DROP COLUMN backfill_covered_id"))
    archive.conn.commit()
    monkeypatch.setattr(q, "_archives_with_backfill_marker", set())
    monkeypatch.setattr(q, "_backfill_marker_missing_warned", False)
    try:
        yield archive
    finally:
        archive.conn.rollback()
        archive.conn.execute(sa.text("ALTER TABLE chats ADD COLUMN backfill_covered_id BIGINT"))
        archive.conn.commit()


def test_an_archive_without_the_column_walks_every_chat_in_full_and_warns_once(archive_without_the_column, telegram, caplog):  # noqa: F811
    with caplog.at_level(logging.WARNING):
        fake, status = telegram({11: ["a", "b"], 12: ["c"]})
        fake2, _ = telegram({11: ["a", "b"], 12: ["c"]})
    assert [c["min_id"] for c in fake.iter_calls + fake2.iter_calls] == [0, 0, 0, 0]
    assert status["state"] == "completed"
    assert caplog.text.count("predates the backfill marker") == 1


# ---------------------------------------------------------------------------
# The migration that adds the column
# ---------------------------------------------------------------------------

REVISION = "b6f2a8d1c394"
PREVIOUS_REVISION = "a9d2e5c7b814"


class TestMigration:
    def test_this_revision_follows_the_previous_one_in_a_single_chain(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1  # (not pinned to this revision: later migrations move the head)
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_109).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
        try:
            # (No probe before the downgrade: a positive answer is cached per database name for the life of the process.)
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert q.chats_have_backfill_marker(conn) is False
                assert q.get_backfill_covered_id(conn, 1) is None  # reading it from an old archive is quiet, not an error
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert q.chats_have_backfill_marker(conn) is True
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()