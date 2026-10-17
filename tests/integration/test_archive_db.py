"""The per-account archive database: provisioning, the write queries the workers use, and the read queries the API uses."""

import pytest
import sqlalchemy as sa

import db
import db.queries as q
import db.read_queries as rq
from tests.helpers import T0, seed_chat


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def chat_row(conn, chat_id):
    return conn.execute(sa.text("SELECT * FROM chats WHERE chat_id = :c"), {"c": chat_id}).mappings().one()


class TestProvisioning:
    def test_schema_was_created_by_the_real_migration_chain(self, archive):
        tables = {r[0] for r in archive.conn.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert {"chats", "senders", "messages", "message_edits", "message_deletions", "backfill_runs", "chat_migrations"} <= tables

    def test_trigram_extension_backs_text_search(self, archive):
        assert scalar(archive.conn, "SELECT COUNT(*) FROM pg_extension WHERE extname = 'pg_trgm'") == 1

    def test_provisioning_twice_is_tolerated(self, server_ready):
        from db.provisioning import provision_archive_database

        first = provision_archive_database(900_003)
        assert provision_archive_database(900_003) == first


class TestWriteQueries:
    def test_chat_type_outside_the_schema_is_rejected(self, archive):
        with pytest.raises(RuntimeError, match="CHECK constraint"):
            q.upsert_chat(archive.conn, chat_id=1, name="x", chat_type="not-a-type")
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chats") == 0  # and the connection is still usable

    def test_upsert_chat_follows_a_rename_without_creating_a_second_row(self, archive):
        q.upsert_chat(archive.conn, chat_id=1, name="Old", chat_type="group", username="old_handle")
        q.upsert_chat(archive.conn, chat_id=1, name="New", chat_type="group", username="new_handle")
        row = chat_row(archive.conn, 1)
        assert (row["name"], row["username"]) == ("New", "new_handle")
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chats") == 1

    def test_a_group_going_private_loses_its_handle(self, archive):
        q.upsert_chat(archive.conn, chat_id=1, name="G", chat_type="supergroup", username="public_group")
        q.upsert_chat(archive.conn, chat_id=1, name="G", chat_type="supergroup", username=None)
        assert chat_row(archive.conn, 1)["username"] is None

    def test_an_unresolved_chat_never_blanks_a_known_name_or_handle(self, archive):
        """name=None means 'lookup failed', not 'has no name' - it must change nothing."""
        q.upsert_chat(archive.conn, chat_id=1, name="Known", chat_type="group", username="handle")
        q.upsert_chat(archive.conn, chat_id=1, name=None, chat_type="group", username=None)
        row = chat_row(archive.conn, 1)
        assert (row["name"], row["username"]) == ("Known", "handle")

    def test_a_rename_keeps_type_first_seen_and_counters(self, archive):
        seed_chat(archive.conn, 1, "Old", chat_type="group", messages=[(1, None, "a")])
        before = chat_row(archive.conn, 1)
        q.upsert_chat(archive.conn, chat_id=1, name="New", chat_type="supergroup")
        after = chat_row(archive.conn, 1)
        assert after["name"] == "New"
        assert (after["chat_type"], after["first_seen"], after["message_count"]) == (before["chat_type"], before["first_seen"], 1)

    def test_insert_message_is_idempotent(self, archive):
        q.upsert_chat(archive.conn, chat_id=1, name="c", chat_type="private")
        first = q.insert_message(archive.conn, tg_message_id=5, chat_id=1, sender_id=None, text="hi", date=T0)
        again = q.insert_message(archive.conn, tg_message_id=5, chat_id=1, sender_id=None, text="hi", date=T0)
        assert first is not None and again is None
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages") == 1

    def test_the_same_telegram_id_in_two_chats_is_two_messages(self, archive):
        for chat_id in (1, 2):
            q.upsert_chat(archive.conn, chat_id=chat_id, name="c", chat_type="private")
            assert q.insert_message(archive.conn, tg_message_id=5, chat_id=chat_id, sender_id=None, text="hi", date=T0) is not None

    def test_chat_counters_follow_inserts_deletes_and_edits(self, archive):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "a"), (2, None, "b"), (3, None, "c")], deleted=[1], edited={2: "b2"})
        row = chat_row(archive.conn, 1)
        assert (row["message_count"], row["deleted_count"], row["edited_count"]) == (3, 1, 1)
        assert row["last_message_preview"] is not None

    def test_get_last_archived_message_id_is_the_backfill_high_water_mark(self, archive):
        assert q.get_last_archived_message_id(archive.conn, 1) is None
        seed_chat(archive.conn, 1, "c", messages=[(10, None, "a"), (30, None, "b"), (20, None, "c")])
        assert q.get_last_archived_message_id(archive.conn, 1) == 30


class TestDeletionInference:
    def _deleted(self, conn, chat_id, tg_id=1):
        return conn.execute(
            sa.text("SELECT d.deleted_by_inference, d.text_snapshot FROM message_deletions d JOIN messages m ON m.id = d.message_id "
                    "WHERE m.chat_id = :c AND m.tg_message_id = :t"),
            {"c": chat_id, "t": tg_id},
        ).one()

    def test_ordinary_chats_stay_unknown_because_telegram_does_not_say_who_deleted(self, archive):
        for chat_id, kind in ((10, "private"), (11, "group"), (12, "supergroup")):
            seed_chat(archive.conn, chat_id, "c", chat_type=kind, messages=[(1, None, "secret")], deleted=[1])
            assert self._deleted(archive.conn, chat_id)[0] == "unknown"

    def test_channel_deletions_are_attributed_to_an_admin(self, archive):
        seed_chat(archive.conn, 20, "news", chat_type="channel", messages=[(1, None, "post")], deleted=[1])
        assert self._deleted(archive.conn, 20)[0] == "channel_admin"

    def test_saved_messages_deletions_are_attributed_to_self(self, archive):
        seed_chat(archive.conn, 777, "Saved", messages=[(1, None, "note")], deleted=[1], self_id=777)
        assert self._deleted(archive.conn, 777)[0] == "self"

    def test_saved_messages_is_not_detected_without_self_id(self, archive):
        seed_chat(archive.conn, 777, "Saved", messages=[(1, None, "note")], deleted=[1])
        assert self._deleted(archive.conn, 777)[0] == "unknown"

    def test_the_deleted_text_is_preserved_in_the_snapshot(self, archive):
        seed_chat(archive.conn, 10, "c", messages=[(1, None, "the original words")], deleted=[1])
        assert self._deleted(archive.conn, 10)[1] == "the original words"

    def test_deleting_an_unknown_or_already_deleted_message_returns_false(self, archive):
        seed_chat(archive.conn, 10, "c", messages=[(1, None, "x")], deleted=[1])
        assert q.flag_deleted(archive.conn, tg_message_id=1, chat_id=10) is False
        assert q.flag_deleted(archive.conn, tg_message_id=999, chat_id=10) is False


class TestEdits:
    def test_edit_keeps_the_previous_text(self, archive):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "first draft")], edited={1: "second draft"})
        detail = rq.get_message_detail(archive.conn, 1)
        assert detail["text"] == "second draft" and detail["is_edited"] is True
        assert [(e["old_text"], e["new_text"]) for e in detail["edits"]] == [("first draft", "second draft")]

    def test_history_accumulates_oldest_first(self, archive):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "v1")])
        q.record_edit(archive.conn, 1, 1, "v2")
        q.record_edit(archive.conn, 1, 1, "v3")
        assert [e["new_text"] for e in rq.get_message_detail(archive.conn, 1)["edits"]] == ["v2", "v3"]

    def test_an_unchanged_text_is_not_logged_as_an_edit(self, archive):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "same")])
        assert q.record_edit(archive.conn, 1, 1, "same") is True
        assert rq.get_message_detail(archive.conn, 1)["edits"] == []

    def test_editing_an_unknown_message_returns_false(self, archive):
        q.upsert_chat(archive.conn, chat_id=1, name="c", chat_type="private")
        assert q.record_edit(archive.conn, 42, 1, "x") is False


class TestChatMigration:
    def test_messages_move_to_the_new_chat_id_and_the_old_row_goes(self, archive):
        seed_chat(archive.conn, -5, "old group", chat_type="group", messages=[(1, None, "a"), (2, None, "b")])
        q.upsert_chat(archive.conn, chat_id=-1005, name="new supergroup", chat_type="supergroup")
        q.record_chat_migration(archive.conn, old_chat_id=-5, new_chat_id=-1005)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages WHERE chat_id = -1005") == 2
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chats WHERE chat_id = -5") == 0

    def test_duplicates_under_both_ids_are_collapsed(self, archive):
        seed_chat(archive.conn, -5, "old", chat_type="group", messages=[(1, None, "dup"), (2, None, "only-old")])
        seed_chat(archive.conn, -1005, "new", chat_type="supergroup", messages=[(1, None, "dup")])
        q.record_chat_migration(archive.conn, old_chat_id=-5, new_chat_id=-1005)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages WHERE chat_id = -1005") == 2
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages WHERE chat_id = -5") == 0

    def test_resolve_chat_id_follows_chains_and_survives_cycles(self, archive):
        for old, new in ((1, 2), (2, 3)):
            archive.conn.execute(sa.text("INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (:o, :n)"), {"o": old, "n": new})
        archive.conn.commit()
        assert q.resolve_chat_id(archive.conn, 1) == 3
        assert q.resolve_chat_id(archive.conn, 99) == 99
        archive.conn.execute(sa.text("INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (3, 1)"))
        archive.conn.commit()
        assert q.resolve_chat_id(archive.conn, 1) in (1, 2, 3)  # must terminate

    def test_recording_the_same_migration_twice_is_a_no_op(self, archive):
        q.upsert_chat(archive.conn, chat_id=-1005, name="n", chat_type="supergroup")
        q.record_chat_migration(archive.conn, -5, -1005)
        q.record_chat_migration(archive.conn, -5, -1005)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chat_migrations") == 1

    def test_a_chat_cannot_migrate_to_itself(self, archive):
        q.record_chat_migration(archive.conn, 5, 5)
        assert scalar(archive.conn, "SELECT COUNT(*) FROM chat_migrations") == 0


@pytest.fixture
def populated(archive):
    seed_chat(archive.conn, 1, "Alice", messages=[(1, 11, "hello world"), (2, 11, "100% sure"), (3, 12, "1000 sure"), (4, 11, "snake_case name")],
              deleted=[2])
    seed_chat(archive.conn, 2, "Team", chat_type="supergroup", messages=[(1, 12, "Hello team"), (2, 13, "concatenate all"), (3, 13, "the cat sat")],
              edited={1: "Hello everyone"})
    seed_chat(archive.conn, 3, "Empty chat")
    return archive.conn


class TestReadQueries:
    def test_pagination_envelope(self, populated):
        page1 = rq.get_messages(populated, page=1, per_page=3)
        page3 = rq.get_messages(populated, page=3, per_page=3)
        assert page1["total"] == 7 and len(page1["items"]) == 3 and len(page3["items"]) == 1

    def test_default_order_is_newest_first_and_asc_flips_it(self, populated):
        desc = [m["text"] for m in rq.get_messages(populated, chat_ids=[1])["items"]]
        asc = [m["text"] for m in rq.get_messages(populated, chat_ids=[1], order="asc")["items"]]
        assert desc == list(reversed(asc)) and asc[0] == "hello world"

    def test_an_unexpected_order_value_falls_back_safely_instead_of_reaching_the_sql(self, populated):
        evil = "asc; DROP TABLE messages; --"
        assert rq.get_messages(populated, order=evil)["total"] == 7
        assert scalar(populated, "SELECT COUNT(*) FROM messages") == 7

    def test_search_is_case_insensitive_substring(self, populated):
        assert {m["text"] for m in rq.get_messages(populated, q="HELLO")["items"]} == {"hello world", "Hello everyone"}

    def test_percent_in_a_search_is_literal_not_a_wildcard(self, populated):
        texts = {m["text"] for m in rq.get_messages(populated, q="100%")["items"]}
        assert texts == {"100% sure"}  # "1000 sure" must NOT match

    def test_underscore_in_a_search_is_literal_not_a_wildcard(self, populated):
        assert {m["text"] for m in rq.get_messages(populated, q="snake_case")["items"]} == {"snake_case name"}
        assert rq.get_messages(populated, q="snake case")["total"] == 0
        assert rq.get_messages(populated, q="snakeXcase")["total"] == 0

    def test_sql_injection_attempts_in_the_search_term_are_inert(self, populated):
        for payload in ("'; DROP TABLE messages; --", "' OR '1'='1", "%' UNION SELECT 1 --", "\\"):
            assert rq.get_messages(populated, q=payload)["total"] == 0
        assert scalar(populated, "SELECT COUNT(*) FROM messages") == 7

    def test_whole_word_search(self, populated):
        assert {m["text"] for m in rq.get_messages(populated, q="cat", whole_word=True)["items"]} == {"the cat sat"}
        assert {m["text"] for m in rq.get_messages(populated, q="cat")["items"]} == {"concatenate all", "the cat sat"}

    def test_chat_filter(self, populated):
        assert rq.get_messages(populated, chat_ids=[2])["total"] == 3
        assert rq.get_messages(populated, chat_ids=[1, 2])["total"] == 7
        assert rq.get_messages(populated, chat_ids=[999])["total"] == 0

    def test_only_deleted_and_only_edited(self, populated):
        assert [m["text"] for m in rq.get_messages(populated, only_deleted=True)["items"]] == ["100% sure"]
        assert [m["text"] for m in rq.get_messages(populated, only_edited=True)["items"]] == ["Hello everyone"]

    def test_sender_filter(self, populated):
        assert rq.get_messages(populated, sender_id=13)["total"] == 2

    def test_date_bounds_are_inclusive(self, populated):
        from datetime import timedelta

        lo, hi = (T0 + timedelta(minutes=1)).isoformat(), (T0 + timedelta(minutes=2)).isoformat()
        assert rq.get_messages(populated, chat_ids=[1], date_from=lo, date_to=hi)["total"] == 2

    def test_rows_are_nested_for_the_api_models(self, populated):
        row = rq.get_messages(populated, q="hello world")["items"][0]
        assert row["chat"]["name"] == "Alice" and row["sender"]["username"] == "user11"

    def test_chat_messages_omit_the_redundant_chat_object(self, populated):
        assert all(m["chat"] is None for m in rq.get_chat_messages(populated, chat_id=1)["items"])

    def test_chats_listing_puts_empty_chats_last_in_both_directions(self, populated):
        for order in ("desc", "asc"):
            names = [c["name"] for c in rq.get_chats(populated, order=order)["items"]]
            assert names[-1] == "Empty chat"

    def test_chat_options_are_alphabetical_case_insensitive(self, populated):
        assert [c["name"] for c in rq.get_chat_options(populated)] == ["Alice", "Empty chat", "Team"]

    def test_get_chat_unknown_returns_none(self, populated):
        assert rq.get_chat(populated, 12345) is None

    def test_message_detail_includes_the_deletion_record(self, populated):
        mid = scalar(populated, "SELECT id FROM messages WHERE text = '100% sure'")
        detail = rq.get_message_detail(populated, mid)
        assert detail["deletion"]["text_snapshot"] == "100% sure" and detail["is_deleted"] is True

    def test_message_detail_unknown_returns_none(self, populated):
        assert rq.get_message_detail(populated, 424242) is None

    def test_stats_totals_come_from_the_denormalised_counters(self, populated):
        stats = rq.get_stats(populated)
        assert (stats["total_messages"], stats["total_deleted"], stats["total_edited"]) == (7, 1, 1)
        assert stats["total_chats"] == 3 and stats["total_senders"] == 3
        assert [c["name"] for c in stats["per_chat"]][0] == "Alice"
        assert stats["archiving_since"] is not None

    def test_message_count(self, populated):
        assert rq.get_message_count(populated) == 7


class TestEmptyArchive:
    def test_stats_are_all_zero_not_an_error(self, archive):
        stats = rq.get_stats(archive.conn)
        assert (stats["total_messages"], stats["total_deleted"], stats["total_edited"], stats["total_chats"], stats["total_senders"]) == (0, 0, 0, 0, 0)
        assert stats["archiving_since"] is None and stats["per_chat"] == []

    def test_listings_are_empty_with_zero_totals(self, archive):
        assert rq.get_messages(archive.conn) == {"items": [], "total": 0}
        assert rq.get_chats(archive.conn) == {"items": [], "total": 0}
        assert rq.get_message_count(archive.conn) == 0


class TestReadOnlyConnection:
    """The API must never be able to modify an archive, even if a route handler had a bug."""

    def test_reads_work(self, archive):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "x")])
        with db.get_tenant_readonly_connection(archive.name) as ro:
            assert rq.get_message_count(ro) == 1

    @pytest.mark.parametrize("statement", [
        "INSERT INTO chats (chat_id, name, chat_type) VALUES (99, 'x', 'private')",
        "UPDATE messages SET text = 'tampered'",
        "DELETE FROM messages",
        "DROP TABLE messages",
        "TRUNCATE messages",
    ])
    def test_every_kind_of_write_is_refused_by_postgres_itself(self, archive, statement):
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "x")])
        with db.get_tenant_readonly_connection(archive.name) as ro:
            with pytest.raises(Exception, match="read-only"):
                ro.execute(sa.text(statement))
        assert scalar(archive.conn, "SELECT COUNT(*) FROM messages") == 1

    def test_the_session_cannot_simply_switch_read_only_off_and_write(self, archive):
        """`SET default_transaction_read_only` is a per-session default; document whether a caller can undo it."""
        seed_chat(archive.conn, 1, "c", messages=[(1, None, "x")])
        with db.get_tenant_readonly_connection(archive.name) as ro:
            try:
                ro.execute(sa.text("SET default_transaction_read_only = off"))
                ro.execute(sa.text("UPDATE messages SET text = 'tampered'"))
                ro.commit()
            except Exception:
                return
        # Reaching here means a bug in a route that ran attacker-controlled SQL could still write: the guard is advisory, not a security boundary.
        pytest.xfail("default_transaction_read_only can be switched off from inside the session (defence in depth only)")


class TestProvisioningSideEffects:
    def test_provisioning_inside_a_running_process_does_not_silence_its_loggers(self, server_ready):
        """
        Regression: alembic's env.py used to call fileConfig() with its default disable_existing_loggers=True.
        Provisioning runs alembic in-process (POST /api/archive/provision), so the first provisioning silently disabled every logger in the API server.
        """
        import logging

        import api.routes.auth  # noqa: F401  - make sure production loggers exist before provisioning
        import api.server  # noqa: F401
        from db.provisioning import provision_archive_database

        names = ["api.server", "api.routes.auth", "db.provisioning", "uvicorn.error"]
        for name in names:
            logging.getLogger(name)
        provision_archive_database(900_002)
        assert [n for n in names if logging.getLogger(n).disabled] == []
