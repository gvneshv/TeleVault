"""
db.queries.clear_all_data(): "forget everything that was stored", at the database level.

The rules under test: every stored thing goes (messages and their edit/deletion text, chats, migrations, backfill history, name history);
settings stay (blocklist, owner, display names); a sender who carries a display name keeps their row (the label hangs on it);
nothing is half-done; and a message that the archiver is storing at that very moment is not lost or left dangling.
"""

import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError

import db.queries as q
import db.read_queries as rq
from sqlalchemy.pool import NullPool

import db
from tests.helpers import seed_chat
from utils.text_matching import fold


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def count(conn, table):
    return scalar(conn, f"SELECT COUNT(*) FROM {table}")


def fill(conn):
    """Two chats with an edit and a deletion, a renamed sender, a label on sender 11, a block rule, the owner, a migration and two backfill runs."""
    seed_chat(conn, 1, "alpha", messages=[(1, 11, "g one"), (2, 11, "g two"), (3, 12, "g three")], deleted=[2], edited={1: "g one edited"})
    seed_chat(conn, 2, "beta", chat_type="group", messages=[(1, 12, "b one"), (2, 13, "b two")], edited={2: "b two edited"})
    q.upsert_sender(conn, sender_id=12, username="user12", first_name="Renamed", last_name=None)  # one name-history row
    q.set_sender_display_name(conn, 11, "Mum")
    q.block_chat(conn, 77, "Blocked one")
    q.record_archive_owner(conn, 4242)
    q.record_chat_migration(conn, 5, 6)
    conn.execute(sa.text("INSERT INTO backfill_runs (status, chat_selector) VALUES ('completed', '1'), ('completed', NULL)"))
    conn.commit()
    return conn


@pytest.fixture
def filled(archive):
    return fill(archive.conn)


@pytest.fixture
def throwaway(server_ready):
    """A private archive database for tests that DROP tables - the shared one must stay intact for every other test.
    Yields a factory of connections."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_090).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestWhatGoes:
    def test_every_stored_thing_is_removed_and_the_counts_say_how_much(self, filled):
        result = q.clear_all_data(filled)
        assert result == {
            "messages_deleted": 5, "edits_deleted": 2, "deletions_deleted": 1, "chats_deleted": 2,
            "senders_deleted": 2, "senders_kept": 1, "backfill_runs_deleted": 2,
        }
        for table in ("messages", "message_edits", "message_deletions", "chats", "chat_migrations", "backfill_runs", "sender_name_history"):
            assert count(filled, table) == 0, table

    def test_the_text_of_edited_and_deleted_messages_is_gone_too(self, filled):
        q.clear_all_data(filled)
        for table, column in (("message_deletions", "text_snapshot"), ("message_edits", "old_text"), ("message_edits", "new_text"), ("messages", "text")):
            assert scalar(filled, f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL") == 0, (table, column)

    def test_an_empty_archive_clears_to_zeros(self, archive):
        assert q.clear_all_data(archive.conn) == {k: 0 for k in (
            "messages_deleted", "edits_deleted", "deletions_deleted", "chats_deleted", "senders_deleted", "senders_kept", "backfill_runs_deleted")}

    def test_the_archive_works_normally_afterwards(self, filled):
        q.clear_all_data(filled)
        seed_chat(filled, 1, "alpha again", messages=[(1, 12, "fresh")])
        chat = rq.get_chat(filled, 1)
        assert chat is not None and chat["message_count"] == 1  # the counters started from zero


class TestWhatStays:
    def test_ignore_rules_are_settings_and_stay(self, filled):
        """Like the blocklist: deleting the stored data must not silently lift the rules that keep unwanted messages out of the archive."""
        q.add_ignore_rule(filled, "тривога", all_chats=False, chats=[(1, "alpha")])
        q.add_ignore_rule(filled, "відбій", all_chats=True)
        q.clear_all_data(filled)
        assert count(filled, "ingest_ignore_rules") == 2 and count(filled, "ingest_ignore_rule_chats") == 1
        assert set(q.get_ignore_patterns(filled, 1)) == {fold("тривога"), fold("відбій")}

    def test_the_blocklist_and_the_owner_are_settings_and_stay(self, filled):
        q.clear_all_data(filled)
        assert q.is_chat_blocked(filled, 77) is True
        assert rq.get_blocked_chats(filled)[0]["name"] == "Blocked one"
        assert rq.get_archive_owner(filled) == 4242

    def test_display_names_stay_and_so_does_the_row_they_hang_on(self, filled):
        """The label has ON DELETE CASCADE to its sender: deleting that sender would silently delete the label."""
        result = q.clear_all_data(filled)
        assert result["senders_kept"] == 1
        assert rq.get_sender_display_names(filled) == [{"sender_id": 11, "display_name": "Mum"}]
        assert {r[0] for r in filled.execute(sa.text("SELECT sender_id FROM senders"))} == {11}

    def test_a_kept_sender_loses_their_name_history_and_the_label_applies_when_they_write_again(self, filled):
        q.upsert_sender(filled, sender_id=11, username="user11", first_name="Changed", last_name=None)
        assert count(filled, "sender_name_history") >= 1
        q.clear_all_data(filled)
        assert count(filled, "sender_name_history") == 0
        detail = rq.get_sender_detail(filled, 11)
        assert detail is not None and detail["display_name"] == "Mum" and len(detail["history"]) == 1
        seed_chat(filled, 3, "new", messages=[(1, 11, "back")])
        assert rq.get_sender_display_names(filled)[0]["display_name"] == "Mum"


class TestOldArchives:
    def test_an_archive_without_the_sender_name_tables_can_still_be_cleared(self, throwaway):
        """Archives that predate the migration must not fail (a missing table would abort the whole transaction); all senders go there."""
        filled = fill(throwaway)
        filled.execute(sa.text("DROP TABLE sender_name_history"))
        filled.execute(sa.text("DROP TABLE sender_display_names"))
        filled.commit()
        result = q.clear_all_data(filled)
        assert result["senders_deleted"] == 3 and result["senders_kept"] == 0
        assert count(filled, "messages") == 0 and count(filled, "senders") == 0


class TestAllOrNothing:
    def test_a_failure_rolls_everything_back_and_the_connection_stays_usable(self, throwaway):
        filled = fill(throwaway)
        filled.execute(sa.text("DROP TABLE backfill_runs"))  # a failure part-way through the sequence
        filled.commit()
        with pytest.raises(ProgrammingError):
            q.clear_all_data(filled)
        assert count(filled, "messages") == 5 and count(filled, "chats") == 2  # the deletes before the failing statement were undone


class TestAnInFlightMessage:
    def test_a_message_being_stored_is_waited_for_and_not_left_dangling(self, archive, filled):
        """
        The archiver has upserted a chat but not committed yet.
        clear_all_data must wait for it (the lock) and then remove it too;
        without the lock the clear would not see that row, finish at once, and the chat would appear right after.
        """
        other = archive.conn.engine.connect()
        try:
            q.upsert_chat(other, chat_id=900, name="in flight", chat_type="private", username=None, commit=False)
            finished = threading.Event()
            outcome: dict = {}

            def run_clear():
                eng = archive.conn.engine
                with eng.connect() as conn:
                    outcome["result"] = q.clear_all_data(conn)
                finished.set()

            worker = threading.Thread(target=run_clear)
            worker.start()
            time.sleep(1.0)
            assert not finished.is_set(), "clear_all_data did not wait for the transaction that was storing a message"
            other.commit()
            worker.join(timeout=20)
            assert finished.is_set()
        finally:
            other.close()
        assert outcome["result"]["chats_deleted"] == 3
        assert count(archive.conn, "chats") == 0