"""The message ignore rules at the database level: the migration, the queries the workers and the API share, and how a rule survives a group upgrade."""

import contextlib
import logging
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

import db
import db.queries as q
import db.read_queries as rq
from tests.conftest import REPO_ROOT
from tests.helpers import seed_chat
from utils.text_matching import fold, matches_any

REVISION = "b94e2d7a1c63"
PREVIOUS_REVISION = "a3f81c5d7e92"


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


@contextlib.contextmanager
def recorded_sql(conn):
    """The SQL statements sent through `conn`'s engine while the block runs.
    The listener is removed again: the archive engine is shared by other tests."""
    statements: list[str] = []

    def listener(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    event.listen(conn.engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(conn.engine, "before_cursor_execute", listener)


def hits(conn, chat_id, text):
    """What the workers do with a message: load the rules for its chat, compare."""
    return matches_any(q.get_ignore_patterns(conn, chat_id), text)


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
        assert cols("ingest_ignore_rules") == {"id": "NO", "pattern": "NO", "pattern_key": "NO", "all_chats": "NO", "created_at": "NO"}
        assert cols("ingest_ignore_rule_chats") == {"rule_id": "NO", "chat_id": "NO", "name": "YES"}

    def test_the_models_and_the_migration_agree(self, archive):
        """Alembic's own drift check: the migration chain must produce exactly what db/schema.py declares for the two tables."""
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        from db.schema import metadata

        ctx = MigrationContext.configure(archive.conn)
        diffs = [d for d in compare_metadata(ctx, metadata) if "ingest_ignore_rule" in repr(d)]
        assert diffs == []

    def test_a_rules_chat_rows_have_no_foreign_key_to_chats(self, archive):
        """Deleting a chat's data must never take a rule's scope with it. (The key to the rule itself is real and cascades.)"""
        targets = {
            r[0]
            for r in archive.conn.execute(
                sa.text(
                    "SELECT ccu.table_name FROM information_schema.table_constraints tc "
                    "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name "
                    "WHERE tc.table_name = 'ingest_ignore_rule_chats' AND tc.constraint_type = 'FOREIGN KEY'"
                )
            )
        }
        assert targets == {"ingest_ignore_rules"}

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """Reverses cleanly on a throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_090).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.ingest_ignore_rules')") is None
                assert scalar(conn, "SELECT to_regclass('public.ingest_ignore_rule_chats')") is None
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert scalar(conn, "SELECT to_regclass('public.ingest_ignore_rules')") is not None
                assert scalar(conn, "SELECT to_regclass('public.ingest_ignore_rule_chats')") is not None
        finally:
            eng.dispose()
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]


# ---------------------------------------------------------------------------
# add / remove / which rules apply
# ---------------------------------------------------------------------------


class TestRules:
    def test_nothing_is_ignored_by_default(self, archive):
        assert q.get_ignore_patterns(archive.conn, 42) == []
        assert rq.get_ignore_rules(archive.conn) == []

    def test_an_all_chats_rule_applies_everywhere(self, archive):
        assert q.add_ignore_rule(archive.conn, "повітряна тривога", all_chats=True) is not None
        assert hits(archive.conn, 1, "ПОВІТРЯНА ТРИВОГА!") is True
        assert hits(archive.conn, 2, "ПОВІТРЯНА ТРИВОГА!") is True

    def test_a_limited_rule_applies_to_its_chats_only(self, archive):
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(10, "Alerts"), (11, "More alerts")])
        assert hits(archive.conn, 10, "тривога") is True
        assert hits(archive.conn, 11, "тривога") is True
        assert hits(archive.conn, 12, "тривога") is False  # a friend's chat is not touched

    def test_the_phrase_is_compared_ignoring_case(self, archive):
        q.add_ignore_rule(archive.conn, "ОГОЛОШЕНА Повітряна Тривога", all_chats=True)
        assert hits(archive.conn, 1, "🚨 М. ХАРКІВ - оголошена повітряна тривога") is True

    def test_the_stored_key_is_the_folded_phrase_and_the_original_is_kept_for_display(self, archive):
        q.add_ignore_rule(archive.conn, "Тривога", all_chats=True)
        row = archive.conn.execute(sa.text("SELECT pattern, pattern_key FROM ingest_ignore_rules")).one()
        assert row == ("Тривога", fold("Тривога"))

    def test_a_second_rule_with_the_same_phrase_in_another_case_is_refused(self, archive):
        assert q.add_ignore_rule(archive.conn, "Тривога", all_chats=True) is not None
        assert q.add_ignore_rule(archive.conn, "ТРИВОГА", all_chats=False, chats=[(10, "A")]) is None
        assert scalar(archive.conn, "SELECT COUNT(*) FROM ingest_ignore_rules") == 1
        assert scalar(archive.conn, "SELECT COUNT(*) FROM ingest_ignore_rule_chats") == 0  # the refused rule left nothing behind

    def test_a_failed_insert_leaves_the_connection_usable(self, archive):
        with pytest.raises(Exception):
            q.add_ignore_rule(archive.conn, "x", all_chats=False, chats=[(2**70, "overflow")])  # outside BIGINT
        assert scalar(archive.conn, "SELECT 1") == 1
        assert scalar(archive.conn, "SELECT COUNT(*) FROM ingest_ignore_rules") == 0  # all-or-nothing: the rule row was rolled back too

    def test_removing_a_rule_stops_it_and_takes_its_chat_rows_along(self, archive):
        rule_id = q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(10, "Alerts")])
        assert rule_id is not None
        assert q.remove_ignore_rule(archive.conn, rule_id) is True
        assert hits(archive.conn, 10, "тривога") is False
        assert scalar(archive.conn, "SELECT COUNT(*) FROM ingest_ignore_rule_chats") == 0

    def test_removing_a_missing_rule_reports_nothing_removed(self, archive):
        assert q.remove_ignore_rule(archive.conn, 12345) is False

    def test_several_rules_all_apply(self, archive):
        q.add_ignore_rule(archive.conn, "тривога", all_chats=True)
        q.add_ignore_rule(archive.conn, "відбій", all_chats=False, chats=[(10, "Alerts")])
        assert hits(archive.conn, 10, "ВІДБІЙ") is True and hits(archive.conn, 10, "тривога") is True
        assert hits(archive.conn, 11, "ВІДБІЙ") is False and hits(archive.conn, 11, "тривога") is True

    def test_a_rule_outlives_the_chat_it_names(self, archive):
        """Deleting a chat's stored data must not silently lift the rule that keeps its noise out."""
        seed_chat(archive.conn, chat_id=10, name="Alerts", messages=[(1, 5, "hello")])
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(10, "Alerts")])
        archive.conn.execute(sa.text("DELETE FROM messages"))
        archive.conn.execute(sa.text("DELETE FROM chats"))
        archive.conn.commit()
        assert hits(archive.conn, 10, "тривога") is True
        assert [(c["chat_id"], c["name"]) for c in rq.get_ignore_rules(archive.conn)[0]["chats"]] == [(10, "Alerts")]

    def test_the_listing_is_newest_first_with_each_rules_chats(self, archive):
        q.add_ignore_rule(archive.conn, "first", all_chats=True)
        archive.conn.execute(sa.text("UPDATE ingest_ignore_rules SET created_at = created_at - interval '1 day'"))
        archive.conn.commit()
        q.add_ignore_rule(archive.conn, "second", all_chats=False, chats=[(11, "B"), (10, "A")])
        rules = rq.get_ignore_rules(archive.conn)
        assert [r["pattern"] for r in rules] == ["second", "first"]
        assert [c["name"] for c in rules[0]["chats"]] == ["A", "B"]
        assert rules[0]["all_chats"] is False and rules[1]["all_chats"] is True and rules[1]["chats"] == []

    def test_a_message_already_stored_is_untouched_by_a_new_rule(self, archive):
        """The owner's decision: a rule only affects what arrives after it exists."""
        seed_chat(archive.conn, chat_id=10, name="Alerts", messages=[(1, 5, "повітряна тривога")])
        q.add_ignore_rule(archive.conn, "повітряна тривога", all_chats=True)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages") == 1


# ---------------------------------------------------------------------------
# Group -> supergroup upgrades
# ---------------------------------------------------------------------------


class TestChatMigration:
    OLD, NEW = -777, -1000000000555

    def test_a_rule_limited_to_a_group_still_applies_after_it_is_upgraded(self, archive):
        """Regression guard for the way a scoped rule silently stops working: the chat gets a new id."""
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(self.OLD, "Book club")])
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert hits(archive.conn, self.NEW, "тривога") is True

    def test_the_row_is_moved_not_duplicated(self, archive):
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(self.OLD, "Book club")])
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert [(c["chat_id"], c["name"]) for c in rq.get_ignore_rules(archive.conn)[0]["chats"]] == [(self.NEW, "Book club")]

    def test_a_late_message_under_the_old_id_still_matches(self, archive):
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(self.OLD, "Book club")])
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert hits(archive.conn, self.OLD, "тривога") is True

    def test_a_rule_that_already_lists_the_new_id_keeps_one_row(self, archive):
        q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(self.OLD, "Old name"), (self.NEW, "New name")])
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        assert [(c["chat_id"], c["name"]) for c in rq.get_ignore_rules(archive.conn)[0]["chats"]] == [(self.NEW, "New name")]

    def test_other_rules_and_chats_are_left_alone(self, archive):
        q.add_ignore_rule(archive.conn, "everywhere", all_chats=True)
        q.add_ignore_rule(archive.conn, "elsewhere", all_chats=False, chats=[(5, "Other")])
        q.record_chat_migration(archive.conn, self.OLD, self.NEW)
        by = {r["pattern"]: r for r in rq.get_ignore_rules(archive.conn)}
        assert by["everywhere"]["all_chats"] is True
        assert [c["chat_id"] for c in by["elsewhere"]["chats"]] == [5]


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def unmigrated(server_ready, monkeypatch):
    """A real archive with the two rule tables dropped again - what every archive created before this feature looks like."""
    from db.provisioning import provision_archive_database

    # The probe remembers "this database has the tables" for the life of the process; a throwaway database must not inherit that from another test.
    monkeypatch.setattr(q, "_archives_with_ignore_rules", set())

    name = provision_archive_database(900_091).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE ingest_ignore_rule_chats"))
        conn.execute(sa.text("DROP TABLE ingest_ignore_rules"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestArchiveWithoutTheTables:
    """Nothing migrates an existing archive automatically, so the workers must keep archiving exactly as before until it is migrated."""

    def test_no_rules_and_the_connection_stays_usable(self, unmigrated, monkeypatch):
        monkeypatch.setattr(q, "_ignore_rules_table_missing_warned", False)
        assert q.get_ignore_patterns(unmigrated, 42) == []
        assert scalar(unmigrated, "SELECT 1") == 1  # the aborted transaction was rolled back, not left poisoning the connection

    def test_no_failing_statement_reaches_the_server(self, unmigrated, monkeypatch):
        """Postgres logs every failing statement as an ERROR, per message, until the archive is migrated - the reason for probing instead of catching the error."""
        monkeypatch.setattr(q, "_ignore_rules_table_missing_warned", False)
        with recorded_sql(unmigrated) as statements:
            q.get_ignore_patterns(unmigrated, 42)
        assert statements and all("to_regclass" in s for s in statements)

    def test_migrating_a_running_system_needs_no_restart(self, unmigrated, monkeypatch):
        """The archiver keeps its process (and the probe's memory) across the migration: the next message must already see the new rules."""
        from db.schema import ingest_ignore_rule_chats, ingest_ignore_rules

        monkeypatch.setattr(q, "_ignore_rules_table_missing_warned", True)
        assert q.get_ignore_patterns(unmigrated, 42) == []
        ingest_ignore_rules.create(unmigrated)
        ingest_ignore_rule_chats.create(unmigrated)
        unmigrated.commit()
        q.add_ignore_rule(unmigrated, "тривога", all_chats=True)
        assert q.get_ignore_patterns(unmigrated, 42) == [fold("тривога")]

    def test_a_present_table_is_probed_once(self, archive, monkeypatch):
        monkeypatch.setattr(q, "_archives_with_ignore_rules", set())
        with recorded_sql(archive.conn) as statements:
            q.get_ignore_patterns(archive.conn, 1)
            q.get_ignore_patterns(archive.conn, 1)
        assert sum("to_regclass" in s for s in statements) == 1

    def test_it_warns_once_not_per_message(self, unmigrated, monkeypatch, caplog):
        monkeypatch.setattr(q, "_ignore_rules_table_missing_warned", False)
        with caplog.at_level(logging.WARNING, logger="db.queries"):
            for _ in range(5):
                q.get_ignore_patterns(unmigrated, 42)
        warnings = [r for r in caplog.records if "ingest_ignore_rules" in r.getMessage()]
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
        so the caller skips the message instead of guessing 'no rules' and archiving something the person asked to ignore.
        """
        unmigrated.execute(sa.text("CREATE TABLE ingest_ignore_rules (id BIGINT)"))
        unmigrated.commit()
        with pytest.raises(ProgrammingError):
            q.get_ignore_patterns(unmigrated, 42)
        assert scalar(unmigrated, "SELECT 1") == 1  # still rolled back

    def test_the_read_and_write_helpers_raise_so_the_api_can_say_409(self, unmigrated):
        with pytest.raises(ProgrammingError) as exc:
            rq.get_ignore_rules(unmigrated)
        assert db.is_undefined_table_error(exc.value)
