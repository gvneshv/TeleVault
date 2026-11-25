"""
The retention policy at the database level: the setting, the preview count, the batch purge and the job that applies them (retention.py).

What these tests pin, because deleting is irreversible: only messages SENT before the cutoff go (and their edit history and deletion snapshots with them);
deleted-in-Telegram messages are spared unless the owner chose otherwise; a run never deletes more than its limit, however many are expired;
a run above the limit is BLOCKED and deletes nothing; chats, the blocklist, the ignore rules and the owner's labels are never touched;
logs carry counts only; and an archive without the table keeps working.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import exc

import db
import db.queries as q
import db.read_queries as rq
import retention
from tests.conftest import REPO_ROOT
from tests.helpers import seed_chat

REVISION = "a9d2e5c7b814"
PREVIOUS_REVISION = "f1c6d8a39b27"
OWNER = 1
OTHER = 50


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def age(conn, chat_id, tg_id, days):
    """Make a stored message `days` days old (the helpers date everything January 2026, which says nothing about today)."""
    conn.execute(
        sa.text("UPDATE messages SET date = now() - make_interval(days => :d) WHERE chat_id = :c AND tg_message_id = :m"),
        {"d": days, "c": chat_id, "m": tg_id},
    )
    conn.commit()


def remaining(conn, chat_id=42):
    return [r[0] for r in conn.execute(sa.text("SELECT tg_message_id FROM messages WHERE chat_id = :c ORDER BY tg_message_id"), {"c": chat_id})]


@pytest.fixture
def chat(archive):
    """Chat 42 with messages 1..5 aged 400, 200, 100, 40 and 1 days: with a 6-month policy 1 and 2 are expired, with 1 month 1..4."""
    conn = archive.conn
    q.record_archive_owner(conn, OWNER)
    seed_chat(conn, chat_id=42, name="Bob", messages=[(i, OTHER, f"text {i}") for i in range(1, 6)])
    for tg_id, days in [(1, 400), (2, 200), (3, 100), (4, 40), (5, 1)]:
        age(conn, 42, tg_id, days)
    return conn


def save(conn, *, enabled=True, months=6, keep_deleted=True, confirmed: int | None = 2):
    q.save_retention_policy(conn, enabled=enabled, months=months, keep_deleted=keep_deleted, confirmed_messages=confirmed)


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

    def test_a_provisioned_archive_has_the_table_and_it_starts_empty(self, chat):
        assert q.retention_table_exists(chat) is True
        assert rq.get_retention_policy(chat) is None

    def test_the_declared_model_has_the_table(self):
        from db.schema import retention_policy

        assert {"singleton", "enabled", "months", "keep_deleted", "confirmed_messages", "updated_at", "last_run_at", "last_run_status", "last_run_deleted"} == set(
            retention_policy.c.keys()
        )

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        """On its own throwaway archive (the shared one must stay at head)."""
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_108).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
        try:
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert q.retention_table_exists(conn) is False
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert q.retention_table_exists(conn) is True
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()

    @pytest.mark.parametrize("months", [0, -1, 121])
    def test_the_database_itself_refuses_a_months_value_outside_one_to_a_hundred_twenty(self, chat, months):
        with pytest.raises(exc.IntegrityError):
            chat.execute(sa.text("INSERT INTO retention_policy (months) VALUES (:m)"), {"m": months})
        chat.rollback()

    def test_the_database_itself_allows_only_one_row(self, chat):
        save(chat)
        with pytest.raises(exc.IntegrityError):
            chat.execute(sa.text("INSERT INTO retention_policy (singleton, months) VALUES (FALSE, 6)"))
        chat.rollback()


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


class TestSetting:
    def test_save_and_read_back(self, chat):
        save(chat, enabled=True, months=6, keep_deleted=False, confirmed=2)
        policy = rq.get_retention_policy(chat)
        assert policy is not None
        assert (policy["enabled"], policy["months"], policy["keep_deleted"], policy["confirmed_messages"]) == (True, 6, False, 2)
        assert (policy["last_run_at"], policy["last_run_status"], policy["last_run_deleted"]) == (None, None, None)

    def test_saving_twice_keeps_one_row_and_moves_updated_at(self, chat):
        save(chat, months=6)
        first = rq.get_retention_policy(chat)["updated_at"]  # type: ignore[index]
        save(chat, months=12, confirmed=0)
        assert scalar(chat, "SELECT COUNT(*) FROM retention_policy") == 1
        policy = rq.get_retention_policy(chat)
        assert policy["months"] == 12 and policy["updated_at"] > first  # type: ignore[index]

    def test_switching_off_keeps_the_number_and_drops_the_confirmation(self, chat):
        save(chat, months=6, confirmed=2)
        save(chat, enabled=False, months=6, confirmed=None)
        policy = rq.get_retention_policy(chat)
        assert (policy["enabled"], policy["months"], policy["confirmed_messages"]) == (False, 6, None)  # type: ignore[index]

    def test_a_run_record_keeps_the_settings_and_can_settle_the_confirmation(self, chat):
        save(chat, confirmed=2)
        q.record_retention_run(chat, status="blocked", deleted=0, clear_confirmation=False)
        assert rq.get_retention_policy(chat)["confirmed_messages"] == 2  # type: ignore[index]
        q.record_retention_run(chat, status="ok", deleted=2, clear_confirmation=True)
        policy = rq.get_retention_policy(chat)
        assert policy is not None
        assert (policy["last_run_status"], policy["last_run_deleted"], policy["confirmed_messages"]) == ("ok", 2, None)
        assert policy["last_run_at"] is not None and policy["months"] == 6

    def test_an_unknown_run_status_is_refused_before_it_reaches_the_database(self, chat):
        save(chat)
        with pytest.raises(ValueError):
            q.record_retention_run(chat, status="great", deleted=0, clear_confirmation=False)


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


class TestPreview:
    def test_it_counts_what_is_older_than_the_cutoff(self, chat):
        result = rq.count_retention_candidates(chat, 6, True)
        assert (result["count"], result["total"]) == (2, 5)
        assert result["oldest"] < result["newest"] < result["cutoff"]

    def test_a_shorter_period_counts_more(self, chat):
        assert rq.count_retention_candidates(chat, 1, True)["count"] == 4
        assert rq.count_retention_candidates(chat, 12, True)["count"] == 1

    def test_nothing_expired_is_zero_with_no_dates(self, chat):
        result = rq.count_retention_candidates(chat, 120, True)
        assert (result["count"], result["oldest"], result["newest"]) == (0, None, None)

    def test_deleted_messages_are_not_counted_when_they_are_kept(self, chat):
        q.flag_deleted(chat, tg_message_id=1, chat_id=42, self_id=None)
        assert rq.count_retention_candidates(chat, 6, True)["count"] == 1
        assert rq.count_retention_candidates(chat, 6, False)["count"] == 2

    def test_it_deletes_nothing(self, chat):
        rq.count_retention_candidates(chat, 1, False)
        assert remaining(chat) == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------


def purge(conn, months=6, *, keep_deleted=True, limit=1000):
    return q.purge_expired_messages(conn, q.retention_cutoff(conn, months), keep_deleted=keep_deleted, limit=limit)


class TestPurge:
    def test_only_expired_messages_go(self, chat):
        result = purge(chat)
        assert remaining(chat) == [3, 4, 5]
        assert result["messages_deleted"] == 2

    def test_the_chat_list_entry_follows_the_messages_that_are_left(self, chat):
        """The counters trigger cannot know the new "last message": without a refresh the chat kept the preview of one that was purged."""
        purge(chat)
        row = chat.execute(sa.text("SELECT message_count, last_message_preview, first_message_at FROM chats WHERE chat_id = 42")).one()
        assert (row[0], row[1]) == (3, "text 5")
        assert row[2] == chat.execute(sa.text("SELECT MIN(date) FROM messages WHERE chat_id = 42")).scalar()

    def test_a_chat_that_lost_every_message_has_no_preview_and_no_dates(self, chat):
        for tg_id in (3, 4, 5):
            age(chat, 42, tg_id, 300)
        purge(chat)
        row = chat.execute(sa.text("SELECT message_count, last_message_preview, first_message_at, last_message_at FROM chats WHERE chat_id = 42")).one()
        assert tuple(row) == (0, None, None, None)

    def test_edit_history_and_deletion_snapshots_go_with_their_message_and_only_theirs(self, chat):
        q.record_edit(chat, tg_message_id=1, chat_id=42, new_text="changed")
        q.record_edit(chat, tg_message_id=3, chat_id=42, new_text="changed")
        q.flag_deleted(chat, tg_message_id=2, chat_id=42, self_id=None)
        result = purge(chat, keep_deleted=False)
        assert (result["edits_deleted"], result["deletions_deleted"]) == (1, 1)
        assert scalar(chat, "SELECT COUNT(*) FROM message_edits") == 1
        assert scalar(chat, "SELECT COUNT(*) FROM message_deletions") == 0

    def test_deleted_messages_are_spared_by_default(self, chat):
        q.flag_deleted(chat, tg_message_id=1, chat_id=42, self_id=None)
        purge(chat, keep_deleted=True)
        assert remaining(chat) == [1, 3, 4, 5]
        assert scalar(chat, "SELECT COUNT(*) FROM message_deletions") == 1

    def test_deleted_messages_go_when_the_owner_chose_so(self, chat):
        q.flag_deleted(chat, tg_message_id=1, chat_id=42, self_id=None)
        purge(chat, keep_deleted=False)
        assert remaining(chat) == [3, 4, 5]

    def test_the_limit_is_never_exceeded_and_the_oldest_go_first(self, chat):
        result = purge(chat, months=1, limit=2)  # 4 are expired
        assert result["messages_deleted"] == 2
        assert remaining(chat) == [3, 4, 5]

    def test_a_zero_limit_deletes_nothing(self, chat):
        assert purge(chat, limit=0)["messages_deleted"] == 0
        assert remaining(chat) == [1, 2, 3, 4, 5]

    def test_small_batches_still_delete_everything_up_to_the_limit(self, chat, monkeypatch):
        monkeypatch.setattr(q, "RETENTION_BATCH_SIZE", 1)
        result = purge(chat, months=1, limit=3)
        assert result["messages_deleted"] == 3
        assert remaining(chat) == [4, 5]

    def test_batches_stop_at_the_end_when_the_limit_is_higher_than_what_is_expired(self, chat, monkeypatch):
        monkeypatch.setattr(q, "RETENTION_BATCH_SIZE", 1)
        assert purge(chat, months=6, limit=100)["messages_deleted"] == 2

    def test_the_chat_counters_follow(self, chat):
        before = scalar(chat, "SELECT message_count FROM chats WHERE chat_id = 42")
        purge(chat)
        assert (before, scalar(chat, "SELECT message_count FROM chats WHERE chat_id = 42")) == (5, 3)

    def test_the_chat_itself_stays_even_when_every_message_is_gone(self, chat):
        chat.execute(sa.text("UPDATE messages SET date = now() - interval '2 years'"))
        chat.commit()
        purge(chat)
        assert remaining(chat) == []
        assert scalar(chat, "SELECT COUNT(*) FROM chats WHERE chat_id = 42") == 1

    def test_a_sender_who_only_wrote_expired_messages_is_removed_one_who_still_has_messages_stays(self, archive):
        conn = archive.conn
        seed_chat(conn, chat_id=42, name="Bob", messages=[(1, 60, "old"), (2, 61, "old"), (3, 61, "new")])
        for tg_id, days in [(1, 400), (2, 400), (3, 1)]:
            age(conn, 42, tg_id, days)
        result = purge(conn)
        assert result["senders_deleted"] == 1
        assert [r[0] for r in conn.execute(sa.text("SELECT sender_id FROM senders"))] == [61]

    def test_a_sender_with_a_display_name_is_kept_with_the_label(self, archive):
        conn = archive.conn
        seed_chat(conn, chat_id=42, name="Bob", messages=[(1, 60, "old")])
        age(conn, 42, 1, 400)
        conn.execute(sa.text("INSERT INTO sender_display_names (sender_id, display_name) VALUES (60, 'Mum')"))
        conn.commit()
        assert purge(conn)["senders_deleted"] == 0
        assert scalar(conn, "SELECT display_name FROM sender_display_names WHERE sender_id = 60") == "Mum"

    def test_the_blocklist_and_the_ignore_rules_are_untouched(self, chat):
        chat.execute(sa.text("INSERT INTO ingest_blocked_chats (chat_id, name) VALUES (777, 'x')"))
        chat.execute(sa.text("INSERT INTO ingest_ignore_rules (pattern, pattern_key) VALUES ('spam', 'spam')"))
        chat.commit()
        purge(chat)
        assert scalar(chat, "SELECT COUNT(*) FROM ingest_blocked_chats") == 1
        assert scalar(chat, "SELECT COUNT(*) FROM ingest_ignore_rules") == 1

    def test_a_failure_in_a_batch_rolls_that_batch_back_and_propagates(self, chat, monkeypatch):
        real_execute = chat.execute

        def failing(statement, *args, **kwargs):
            if "DELETE FROM messages" in str(statement):
                raise exc.OperationalError("boom", {}, Exception("boom"))
            return real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(chat, "execute", failing)
        with pytest.raises(exc.OperationalError):
            purge(chat)
        monkeypatch.undo()
        assert remaining(chat) == [1, 2, 3, 4, 5]
        assert scalar(chat, "SELECT COUNT(*) FROM message_deletions") == 0


# ---------------------------------------------------------------------------
# The cutoff the backfill uses
# ---------------------------------------------------------------------------


class TestActiveCutoff:
    def test_no_policy_means_no_cutoff(self, chat):
        assert q.active_retention_cutoff(chat) is None

    def test_a_switched_off_policy_means_no_cutoff(self, chat):
        save(chat, enabled=False, confirmed=None)
        assert q.active_retention_cutoff(chat) is None

    def test_an_enabled_policy_gives_its_cutoff(self, chat):
        save(chat, months=6)
        cutoff = q.active_retention_cutoff(chat)
        assert cutoff is not None
        assert timedelta(days=170) < datetime.now(timezone.utc) - cutoff < timedelta(days=190)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@pytest.fixture
def strict(monkeypatch):
    """No tolerance and a tiny steady-state limit, so a handful of rows can prove the limits (the real floors are 1000 and 10000)."""
    import utils.retention_rules as rules

    monkeypatch.setattr(rules, "RETENTION_CONFIRM_TOLERANCE_MIN", 0)
    monkeypatch.setattr(rules, "RETENTION_CONFIRM_TOLERANCE_FRACTION", 0.0)
    monkeypatch.setattr(rules, "RETENTION_STEADY_RUN_MIN", 1)
    monkeypatch.setattr(rules, "RETENTION_STEADY_RUN_FRACTION", 0.0)


def status_of(conn):
    policy = rq.get_retention_policy(conn)
    return None if policy is None else (policy["last_run_status"], policy["last_run_deleted"], policy["confirmed_messages"])


class TestJob:
    def test_nothing_configured_does_nothing(self, chat):
        assert retention.apply_policy_once(chat) is None
        assert remaining(chat) == [1, 2, 3, 4, 5]

    def test_a_switched_off_policy_does_nothing(self, chat):
        save(chat, enabled=False, confirmed=None)
        assert retention.apply_policy_once(chat) is None
        assert remaining(chat) == [1, 2, 3, 4, 5]

    def test_a_confirmed_first_run_deletes_and_settles_the_confirmation(self, chat):
        save(chat, confirmed=2)
        assert retention.apply_policy_once(chat) == "ok"
        assert remaining(chat) == [3, 4, 5]
        assert status_of(chat) == ("ok", 2, None)

    def test_a_second_call_within_a_day_is_not_due(self, chat):
        save(chat, confirmed=2)
        retention.apply_policy_once(chat)
        assert retention.apply_policy_once(chat) is None

    def test_it_runs_again_after_a_day(self, chat):
        save(chat, confirmed=2)
        retention.apply_policy_once(chat)
        age(chat, 42, 3, 300)  # became expired since
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        assert retention.apply_policy_once(chat, now=later) == "ok"
        assert remaining(chat) == [4, 5]

    def test_a_run_above_the_confirmed_count_is_blocked_and_deletes_nothing(self, chat, strict):
        save(chat, confirmed=1)  # the owner saw 1, but 2 are expired
        assert retention.apply_policy_once(chat) == "blocked"
        assert remaining(chat) == [1, 2, 3, 4, 5]
        assert status_of(chat) == ("blocked", 0, 1)  # the confirmation is kept: nothing was settled

    def test_a_blocked_run_stays_blocked_until_the_policy_is_saved_again(self, chat, strict):
        save(chat, confirmed=1)
        retention.apply_policy_once(chat)
        later = datetime.now(timezone.utc) + timedelta(days=30)
        assert retention.apply_policy_once(chat, now=later) is None
        save(chat, confirmed=2)  # the owner looked at the new preview
        assert retention.apply_policy_once(chat) == "ok"
        assert remaining(chat) == [3, 4, 5]

    def test_a_steady_run_that_would_delete_too_much_is_blocked(self, chat, strict):
        """After the confirmed first run: a clock jump or a long pause must not wipe a lot in one go."""
        save(chat, months=6, confirmed=2)
        retention.apply_policy_once(chat)
        age(chat, 42, 4, 300)
        age(chat, 42, 5, 300)  # two more expired, the steady limit (strict) is 1
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        assert retention.apply_policy_once(chat, now=later) == "blocked"
        assert remaining(chat) == [3, 4, 5]

    def test_a_failure_is_recorded_as_an_error_and_nothing_is_deleted(self, chat, monkeypatch):
        save(chat, confirmed=2)

        def boom(*args, **kwargs):
            raise exc.OperationalError("boom", {}, Exception("boom"))

        monkeypatch.setattr(q, "purge_expired_messages", boom)
        assert retention.apply_policy_once(chat) == "error"
        assert remaining(chat) == [1, 2, 3, 4, 5]
        assert status_of(chat) == ("error", 0, 2)

    def test_the_logs_carry_counts_only(self, chat, caplog):
        save(chat, confirmed=2)
        with caplog.at_level(logging.DEBUG):
            retention.apply_policy_once(chat)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "2 messages" in text
        assert "text 1" not in text and "Bob" not in text

    def test_keep_deleted_is_honoured_by_the_job(self, chat):
        q.flag_deleted(chat, tg_message_id=1, chat_id=42, self_id=None)
        save(chat, keep_deleted=True, confirmed=1)
        assert retention.apply_policy_once(chat) == "ok"
        assert remaining(chat) == [1, 3, 4, 5]


# ---------------------------------------------------------------------------
# An archive that has not been migrated yet
# ---------------------------------------------------------------------------


@pytest.fixture
def without_table(server_ready):
    """A real archive that has everything but the retention table (every archive before this change)."""
    from db.provisioning import provision_archive_database

    name = provision_archive_database(900_109).db_name
    eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE retention_policy"))
    with eng.connect() as conn:
        yield conn
    eng.dispose()


class TestUnmigratedArchive:
    def test_the_job_skips_quietly_with_one_warning_not_one_per_minute(self, without_table, monkeypatch, caplog):
        monkeypatch.setattr(retention, "_outdated_warned", False)
        with caplog.at_level(logging.WARNING, logger="retention"):
            assert retention.apply_policy_once(without_table) is None
            assert retention.apply_policy_once(without_table) is None
        assert len([r for r in caplog.records if "retention table" in r.getMessage()]) == 1

    def test_the_backfill_cutoff_is_none_and_the_connection_stays_usable(self, without_table):
        assert q.active_retention_cutoff(without_table) is None
        assert scalar(without_table, "SELECT 1") == 1


class TestRepairOfStaleListEntries:
    """Chats emptied before refresh_chat_summaries() existed still carry their old preview; the archiver heals them once at start."""

    def stale(self, conn):
        seed_chat(conn, chat_id=77, name="Old", messages=[(1, OTHER, "forgotten")])
        conn.execute(sa.text("DELETE FROM messages WHERE chat_id = 77"))  # what an old cleanup did: the trigger fixes the counts, nothing else
        conn.commit()

    def test_an_emptied_chat_keeps_its_old_entry_until_repaired(self, chat):
        self.stale(chat)
        assert scalar(chat, "SELECT last_message_preview FROM chats WHERE chat_id = 77") == "forgotten"
        assert q.repair_stale_chat_summaries(chat) == 1
        row = chat.execute(sa.text("SELECT message_count, last_message_preview, last_message_at, first_message_at FROM chats WHERE chat_id = 77")).one()
        assert tuple(row) == (0, None, None, None)

    def test_a_healthy_archive_is_left_alone(self, chat):
        before = chat.execute(sa.text("SELECT chat_id, last_message_preview, last_message_at FROM chats ORDER BY chat_id")).all()
        assert q.repair_stale_chat_summaries(chat) == 0
        assert chat.execute(sa.text("SELECT chat_id, last_message_preview, last_message_at FROM chats ORDER BY chat_id")).all() == before

    def test_the_archiver_start_does_it(self, chat, monkeypatch):
        from contextlib import nullcontext

        self.stale(chat)
        monkeypatch.setattr(db, "get_connection", lambda: nullcontext(chat))  # the job opens its own connection; hand it the test's
        retention._repair_summaries()
        assert scalar(chat, "SELECT last_message_preview FROM chats WHERE chat_id = 77") is None