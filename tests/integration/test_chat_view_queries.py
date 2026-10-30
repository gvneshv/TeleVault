"""
The read/write queries behind the chat view (docs: claude/chat-view-spec.md, step A):

    - edit_count on message rows and edited_count on chats (so a list can say "Edited twice" without a request per message),
    - the anchored, cursor-based window of one chat's messages (db/read_queries.get_chat_window),
    - purging every stored trace of one chat (db/queries.purge_chat).

Every test here ran red before the matching code existed; see the CHANGELOG entry for the chat view for what each rule protects.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

import db.queries as q
import db.read_queries as rq
from tests.helpers import T0, seed_chat


def scalar(conn, sql, **params):
    return conn.execute(sa.text(sql), params).scalar()


def edit_twice(conn, chat_id: int, tg_message_id: int) -> None:
    """Two edits of one message -> two rows in message_edits (seed_chat's `edited=` applies only one per message)."""
    q.record_edit(conn, tg_message_id=tg_message_id, chat_id=chat_id, new_text="second version")
    q.record_edit(conn, tg_message_id=tg_message_id, chat_id=chat_id, new_text="third version")


@pytest.fixture
def edited_chat(archive):
    """Chat 1: message 1 untouched, message 2 edited twice, message 3 edited once and message 4 deleted."""
    conn = archive.conn
    seed_chat(conn, 1, "Alice", messages=[(1, 11, "plain"), (2, 11, "v1"), (3, 12, "w1"), (4, 11, "gone")], edited={3: "w2"}, deleted=[4])
    edit_twice(conn, 1, 2)
    return conn


# ---------------------------------------------------------------------------
# A1 - edit counts
# ---------------------------------------------------------------------------


class TestEditCount:
    def test_message_rows_say_how_many_times_they_were_edited(self, edited_chat):
        counts = {m["tg_message_id"]: m["edit_count"] for m in rq.get_chat_messages(edited_chat, chat_id=1, order="asc")["items"]}
        assert counts == {1: 0, 2: 2, 3: 1, 4: 0}

    def test_the_global_feed_carries_it_too(self, edited_chat):
        counts = {m["tg_message_id"]: m["edit_count"] for m in rq.get_messages(edited_chat)["items"]}
        assert counts == {1: 0, 2: 2, 3: 1, 4: 0}

    def test_the_detail_view_agrees_with_its_own_edit_history(self, edited_chat):
        message_id = scalar(edited_chat, "SELECT id FROM messages WHERE chat_id = 1 AND tg_message_id = 2")
        detail = rq.get_message_detail(edited_chat, message_id)
        assert detail is not None
        assert detail["edit_count"] == len(detail["edits"]) == 2

    def test_whole_word_search_rows_keep_the_count(self, edited_chat):
        """Whole-word search takes a different code path (_paginate_filtered); it must not lose the field."""
        rows = rq.get_chat_messages(edited_chat, chat_id=1, q="version", whole_word=True)["items"]
        assert [r["edit_count"] for r in rows] == [2]

    def test_one_message_does_not_borrow_anothers_edits(self, archive):
        """Same Telegram id in two chats: the count follows the internal message id, not tg_message_id."""
        seed_chat(archive.conn, 1, "A", messages=[(1, None, "a")], edited={1: "a2"})
        seed_chat(archive.conn, 2, "B", messages=[(1, None, "b")])
        counts = {m["chat"]["chat_id"]: m["edit_count"] for m in rq.get_messages(archive.conn)["items"]}
        assert counts == {1: 1, 2: 0}


class TestChatEditedCount:
    def test_chats_expose_the_edited_counter_that_already_exists_in_the_archive(self, edited_chat):
        chat = rq.get_chat(edited_chat, 1)
        assert chat is not None
        assert (chat["message_count"], chat["edited_count"], chat["deleted_count"]) == (4, 2, 1)
        listed = rq.get_chats(edited_chat)["items"][0]
        assert listed["edited_count"] == 2


# ---------------------------------------------------------------------------
# A2 - the anchored, cursor-based window
# ---------------------------------------------------------------------------


@pytest.fixture
def ten(archive):
    """Chat 1 holds messages "m1".."m10" one minute apart (oldest first); chat 2 holds three more - used to prove a cursor cannot cross chats."""
    seed_chat(archive.conn, 1, "Ten", messages=[(i, 11, f"m{i}") for i in range(1, 11)])
    seed_chat(archive.conn, 2, "Other", messages=[(i, 12, f"o{i}") for i in range(1, 4)])
    return archive.conn


def mid(conn, chat_id: int, text: str) -> int:
    """Internal id of the message with this text - the cursor the API hands out and takes back."""
    return scalar(conn, "SELECT id FROM messages WHERE chat_id = :c AND text = :t", c=chat_id, t=text)


def texts(window) -> list[str]:
    return [m["text"] for m in window["items"]]


class TestWindowNewest:
    def test_without_an_anchor_it_is_the_newest_messages_oldest_first(self, ten):
        w = rq.get_chat_window(ten, 1, limit=4)
        assert w is not None
        assert texts(w) == ["m7", "m8", "m9", "m10"]  # the order a conversation is read in
        assert (w["has_older"], w["has_newer"], w["anchor_id"]) == (True, False, None)

    def test_a_limit_larger_than_the_chat_returns_everything_and_says_nothing_is_older(self, ten):
        w = rq.get_chat_window(ten, 1, limit=50)
        assert w is not None and len(w["items"]) == 10 and w["has_older"] is False

    def test_exactly_the_whole_chat_is_not_reported_as_having_older_messages(self, ten):
        w = rq.get_chat_window(ten, 1, limit=10)
        assert w is not None and len(w["items"]) == 10 and w["has_older"] is False

    def test_an_empty_chat_gives_an_empty_window(self, archive):
        seed_chat(archive.conn, 3, "Empty")
        assert rq.get_chat_window(archive.conn, 3, limit=10) == {"items": [], "has_older": False, "has_newer": False, "anchor_id": None}

    def test_rows_carry_edit_counts_and_no_chat_object(self, archive):
        seed_chat(archive.conn, 1, "A", messages=[(1, 11, "x")], edited={1: "x2"})
        w = rq.get_chat_window(archive.conn, 1, limit=5)
        assert w is not None
        assert w["items"][0]["edit_count"] == 1 and w["items"][0]["chat"] is None


class TestWindowAround:
    def test_a_message_sits_in_the_middle_with_both_sides_open(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, around=mid(ten, 1, "m5"))
        assert w is not None
        assert texts(w) == ["m3", "m4", "m5", "m6", "m7"]
        assert (w["has_older"], w["has_newer"], w["anchor_id"]) == (True, True, mid(ten, 1, "m5"))

    def test_near_the_start_the_older_side_ends(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, around=mid(ten, 1, "m2"))
        assert w is not None
        assert texts(w) == ["m1", "m2", "m3", "m4"]
        assert (w["has_older"], w["has_newer"]) == (False, True)

    def test_at_the_newest_message_the_newer_side_ends(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, around=mid(ten, 1, "m10"))
        assert w is not None
        assert texts(w) == ["m8", "m9", "m10"] and (w["has_older"], w["has_newer"]) == (True, False)

    def test_a_limit_of_one_is_just_the_anchor(self, ten):
        w = rq.get_chat_window(ten, 1, limit=1, around=mid(ten, 1, "m5"))
        assert w is not None
        assert texts(w) == ["m5"] and (w["has_older"], w["has_newer"]) == (True, True)

    def test_the_window_is_exact_not_off_by_one_at_its_edges(self, ten):
        """m5 +/- 2 is a complete window: m2 and m8 are outside it, and the flags must say there is more beyond."""
        w = rq.get_chat_window(ten, 1, limit=5, around=mid(ten, 1, "m5"))
        assert w is not None and "m2" not in texts(w) and "m8" not in texts(w)


class TestWindowCursors:
    def test_before_returns_the_messages_just_older_than_the_cursor(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, before=mid(ten, 1, "m5"))
        assert w is not None
        assert texts(w) == ["m2", "m3", "m4"] and (w["has_older"], w["has_newer"]) == (True, True)

    def test_before_the_first_messages_reports_the_end_of_history(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, before=mid(ten, 1, "m3"))
        assert w is not None
        assert texts(w) == ["m1", "m2"] and w["has_older"] is False

    def test_before_the_very_first_message_is_empty(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, before=mid(ten, 1, "m1"))
        assert w is not None and w["items"] == [] and w["has_older"] is False

    def test_after_returns_the_messages_just_newer_than_the_cursor(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, after=mid(ten, 1, "m5"))
        assert w is not None
        assert texts(w) == ["m6", "m7", "m8"] and (w["has_older"], w["has_newer"]) == (True, True)

    def test_after_the_last_messages_reports_the_end(self, ten):
        w = rq.get_chat_window(ten, 1, limit=5, after=mid(ten, 1, "m8"))
        assert w is not None
        assert texts(w) == ["m9", "m10"] and w["has_newer"] is False

    def test_paging_back_to_the_start_shows_every_message_exactly_once(self, ten):
        seen: list[str] = []
        w = rq.get_chat_window(ten, 1, limit=3)
        assert w is not None
        while True:
            seen = texts(w) + seen
            if not w["has_older"]:
                break
            w = rq.get_chat_window(ten, 1, limit=3, before=w["items"][0]["id"])
            assert w is not None
        assert seen == [f"m{i}" for i in range(1, 11)]

    def test_messages_sharing_one_timestamp_are_neither_skipped_nor_repeated(self, archive):
        """Backfill stores whole bursts with the same second; the (date, id) key must still be a total order."""
        q.upsert_chat(archive.conn, chat_id=1, name="Burst", chat_type="private")
        for i in range(1, 8):
            q.insert_message(archive.conn, tg_message_id=i, chat_id=1, sender_id=None, text=f"b{i}", date=T0)
        seen: list[str] = []
        w = rq.get_chat_window(archive.conn, 1, limit=2)
        assert w is not None
        while True:
            seen = texts(w) + seen
            if not w["has_older"]:
                break
            w = rq.get_chat_window(archive.conn, 1, limit=2, before=w["items"][0]["id"])
            assert w is not None
        assert seen == [f"b{i}" for i in range(1, 8)]

    def test_forwards_and_backwards_meet_without_a_gap(self, ten):
        anchor = mid(ten, 1, "m5")
        older = rq.get_chat_window(ten, 1, limit=2, before=anchor)
        newer = rq.get_chat_window(ten, 1, limit=2, after=anchor)
        assert older is not None and newer is not None
        assert texts(older) + ["m5"] + texts(newer) == ["m3", "m4", "m5", "m6", "m7"]


class TestWindowByDate:
    def test_an_exact_timestamp_anchors_on_that_message(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, around_date=T0 + timedelta(minutes=4))  # m5
        assert w is not None
        assert w["anchor_id"] == mid(ten, 1, "m5") and texts(w) == ["m4", "m5", "m6"]

    def test_a_date_between_two_messages_anchors_on_the_next_one(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, around_date=T0 + timedelta(minutes=4, seconds=30))
        assert w is not None and w["anchor_id"] == mid(ten, 1, "m6")

    def test_a_date_before_everything_anchors_on_the_first_message(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, around_date=T0 - timedelta(days=30))
        assert w is not None
        assert w["anchor_id"] == mid(ten, 1, "m1") and w["has_older"] is False

    def test_a_date_after_everything_anchors_on_the_last_message(self, ten):
        w = rq.get_chat_window(ten, 1, limit=3, around_date=T0 + timedelta(days=30))
        assert w is not None
        assert w["anchor_id"] == mid(ten, 1, "m10") and w["has_newer"] is False

    def test_a_date_in_an_empty_chat_gives_an_empty_window(self, archive):
        seed_chat(archive.conn, 3, "Empty")
        w = rq.get_chat_window(archive.conn, 3, limit=3, around_date=T0)
        assert w == {"items": [], "has_older": False, "has_newer": False, "anchor_id": None}


# One entry per message-id anchor, spelled out (a **{param: id} spread would hide the keyword from the type checker).
ANCHORS = {
    "around": lambda conn, message_id: rq.get_chat_window(conn, 1, limit=5, around=message_id),
    "before": lambda conn, message_id: rq.get_chat_window(conn, 1, limit=5, before=message_id),
    "after": lambda conn, message_id: rq.get_chat_window(conn, 1, limit=5, after=message_id),
}


class TestWindowIsolation:
    @pytest.mark.parametrize("param", list(ANCHORS))
    def test_a_message_of_another_chat_is_not_a_valid_anchor(self, ten, param):
        assert ANCHORS[param](ten, mid(ten, 2, "o2")) is None

    @pytest.mark.parametrize("param", list(ANCHORS))
    def test_an_unknown_message_id_is_not_a_valid_anchor(self, ten, param):
        assert ANCHORS[param](ten, 987_654) is None

    def test_other_chats_never_leak_into_a_window(self, ten):
        w = rq.get_chat_window(ten, 1, limit=50)
        assert w is not None and all(t.startswith("m") for t in texts(w))

    def test_more_than_one_anchor_is_a_programming_error(self, ten):
        with pytest.raises(ValueError, match="at most one"):
            rq.get_chat_window(ten, 1, limit=5, around=1, before=2)


# ---------------------------------------------------------------------------
# A3 - purging everything stored for one chat
# ---------------------------------------------------------------------------


def count(conn, table: str) -> int:
    return scalar(conn, f"SELECT COUNT(*) FROM {table}")


@pytest.fixture
def two_chats(archive):
    """
    Chat 1 "Gone" (what gets purged) and chat 2 "Keep" (what must be left exactly as it was).
    Sender 11 only ever wrote in chat 1; sender 12 wrote in both; sender 13 only in chat 2.
    Chat 1 has an edited message (two edits) and a deleted message; chat 2 has one edited message.
    """
    conn = archive.conn
    seed_chat(conn, 1, "Gone", messages=[(1, 11, "g1"), (2, 12, "g2"), (3, 11, "g3"), (4, 12, "g4")], edited={1: "g1 v2"}, deleted=[3])
    q.record_edit(conn, tg_message_id=1, chat_id=1, new_text="g1 v3")
    seed_chat(conn, 2, "Keep", messages=[(1, 12, "k1"), (2, 13, "k2")], edited={2: "k2 v2"})
    return conn


def snapshot_of_chat_2(conn) -> list:
    """Everything stored about chat 2, as plain tuples - compared before and after a purge of chat 1."""
    rows = conn.execute(sa.text("SELECT * FROM chats WHERE chat_id = 2")).all()
    rows += conn.execute(sa.text("SELECT id, tg_message_id, chat_id, sender_id, text, is_edited, is_deleted FROM messages WHERE chat_id = 2 ORDER BY id")).all()
    rows += conn.execute(sa.text("SELECT e.old_text, e.new_text FROM message_edits e JOIN messages m ON m.id = e.message_id WHERE m.chat_id = 2 ORDER BY e.id")).all()
    return [tuple(r) for r in rows]


class TestPurgeChat:
    def test_everything_of_that_chat_is_gone_and_the_counts_say_how_much(self, two_chats):
        result = q.purge_chat(two_chats, 1)
        assert result is not None
        assert result["chat_id"] == 1
        assert (result["messages_deleted"], result["edits_deleted"], result["deletions_deleted"]) == (4, 2, 1)
        assert scalar(two_chats, "SELECT COUNT(*) FROM chats WHERE chat_id = 1") == 0
        assert scalar(two_chats, "SELECT COUNT(*) FROM messages WHERE chat_id = 1") == 0
        assert scalar(two_chats, "SELECT COUNT(*) FROM message_edits e JOIN messages m ON m.id = e.message_id WHERE m.chat_id = 1") == 0
        # No edit or deletion row may be left pointing at a message that no longer exists.
        assert scalar(two_chats, "SELECT COUNT(*) FROM message_edits WHERE message_id NOT IN (SELECT id FROM messages)") == 0
        assert scalar(two_chats, "SELECT COUNT(*) FROM message_deletions WHERE message_id NOT IN (SELECT id FROM messages)") == 0

    def test_the_text_of_deleted_and_edited_messages_is_gone_too(self, two_chats):
        """A purge that left the deletion snapshot or the old edit text behind would defeat its whole point."""
        q.purge_chat(two_chats, 1)
        for table, column in (("message_deletions", "text_snapshot"), ("message_edits", "old_text"), ("message_edits", "new_text"), ("messages", "text")):
            assert scalar(two_chats, f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE 'g%'") == 0, (table, column)

    def test_other_chats_are_untouched(self, two_chats):
        before = snapshot_of_chat_2(two_chats)
        q.purge_chat(two_chats, 1)
        assert snapshot_of_chat_2(two_chats) == before
        chat = rq.get_chat(two_chats, 2)
        assert chat is not None and (chat["message_count"], chat["edited_count"]) == (2, 1)

    def test_a_sender_who_wrote_only_there_is_removed_but_a_shared_one_stays(self, two_chats):
        result = q.purge_chat(two_chats, 1)
        assert result is not None and result["senders_deleted"] == 1
        remaining = {r[0] for r in two_chats.execute(sa.text("SELECT sender_id FROM senders"))}
        assert remaining == {12, 13}

    def test_a_sender_with_no_messages_elsewhere_in_the_archive_is_not_swept_up_by_accident(self, two_chats):
        """Only senders of the purged chat are candidates: an unrelated orphan row must not be deleted as a side effect."""
        q.upsert_sender(two_chats, sender_id=99, username="orphan", first_name="Orphan", last_name=None)
        q.purge_chat(two_chats, 1)
        assert scalar(two_chats, "SELECT COUNT(*) FROM senders WHERE sender_id = 99") == 1

    def test_an_unknown_chat_returns_none_and_changes_nothing(self, two_chats):
        before = (count(two_chats, "chats"), count(two_chats, "messages"), count(two_chats, "senders"))
        assert q.purge_chat(two_chats, 424242) is None
        assert (count(two_chats, "chats"), count(two_chats, "messages"), count(two_chats, "senders")) == before

    def test_purging_twice_is_a_clean_no_op_the_second_time(self, two_chats):
        assert q.purge_chat(two_chats, 1) is not None
        assert q.purge_chat(two_chats, 1) is None

    def test_a_chat_with_no_messages_can_be_purged(self, archive):
        seed_chat(archive.conn, 5, "Empty")
        result = q.purge_chat(archive.conn, 5)
        assert result is not None and result["messages_deleted"] == 0
        assert count(archive.conn, "chats") == 0

    def test_a_new_message_after_the_purge_creates_the_chat_again_from_zero(self, two_chats):
        """What the owner asked for: the chat only returns if someone writes again while the archiver runs - and then without any old history."""
        q.purge_chat(two_chats, 1)
        seed_chat(two_chats, 1, "Back again", messages=[(50, 11, "hello again")])
        chat = rq.get_chat(two_chats, 1)
        assert chat is not None and (chat["name"], chat["message_count"], chat["edited_count"], chat["deleted_count"]) == ("Back again", 1, 0, 0)

    def test_the_counter_trigger_copes_with_a_big_chat(self, archive):
        """A delete fires the per-row counter trigger once per message; a few thousand rows must go through in one transaction."""
        seed_chat(archive.conn, 7, "Big")
        archive.conn.execute(
            sa.text(
                "INSERT INTO messages (tg_message_id, chat_id, text, date) "
                "SELECT g, 7, 'bulk ' || g, now() - make_interval(secs => g) FROM generate_series(1, 5000) AS g"
            )
        )
        archive.conn.commit()
        assert rq.get_chat(archive.conn, 7)["message_count"] == 5000  # type: ignore[index]
        result = q.purge_chat(archive.conn, 7)
        assert result is not None and result["messages_deleted"] == 5000
        assert count(archive.conn, "messages") == 0 and count(archive.conn, "chats") == 0


class TestPurgeKeepsWhatItShould:
    def test_a_block_rule_survives_and_the_result_says_so(self, two_chats):
        """Block-and-forget must not silently un-block: the rule is the only thing that stops the chat refilling."""
        q.block_chat(two_chats, 1, "Gone")
        result = q.purge_chat(two_chats, 1)
        assert result is not None and result["block_rule_kept"] is True
        assert q.is_chat_blocked(two_chats, 1) is True
        assert rq.get_blocked_chats(two_chats)[0]["name"] == "Gone"

    def test_without_a_rule_the_result_says_there_was_none(self, two_chats):
        result = q.purge_chat(two_chats, 1)
        assert result is not None and result["block_rule_kept"] is False

    def test_an_archive_that_predates_the_blocklist_table_can_still_purge(self, two_chats):
        """Old archives have no ingest_blocked_chats table until scripts/migrate_archives.py runs; a purge must not need it."""
        two_chats.execute(sa.text("ALTER TABLE ingest_blocked_chats RENAME TO ingest_blocked_chats_moved"))
        two_chats.commit()
        try:
            result = q.purge_chat(two_chats, 1)
        finally:
            two_chats.rollback()
            two_chats.execute(sa.text("ALTER TABLE ingest_blocked_chats_moved RENAME TO ingest_blocked_chats"))
            two_chats.commit()
        assert result is not None and result["block_rule_kept"] is False and count(two_chats, "messages") == 2


class TestPurgeOfLinkedRecords:
    def test_group_migration_records_that_mention_the_chat_are_removed_unrelated_ones_stay(self, two_chats):
        two_chats.execute(sa.text("INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (500, 1), (1, 600), (700, 701)"))
        two_chats.commit()
        q.purge_chat(two_chats, 1)
        assert [tuple(r) for r in two_chats.execute(sa.text("SELECT old_chat_id, new_chat_id FROM chat_migrations"))] == [(700, 701)]

    def test_only_backfill_runs_that_targeted_this_chat_are_removed(self, two_chats):
        q.upsert_chat(two_chats, chat_id=1, name="Gone", chat_type="private", username="gone_user")
        for selector in ("1", "@gone_user", "GONE_USER", "2", "@someone_else", None):
            two_chats.execute(
                sa.text("INSERT INTO backfill_runs (status, chat_selector, messages_stored) VALUES ('completed', :s, 3)"), {"s": selector}
            )
        two_chats.commit()
        result = q.purge_chat(two_chats, 1)
        assert result is not None and result["backfill_runs_deleted"] == 3
        left = sorted(str(r[0]) for r in two_chats.execute(sa.text("SELECT chat_selector FROM backfill_runs")))
        assert left == sorted(["2", "@someone_else", "None"])  # a run over EVERY chat (selector NULL) is a global log entry and stays


class TestPurgeIsAllOrNothing:
    def test_a_failure_in_the_last_step_rolls_everything_back(self, two_chats):
        """
        A real failing statement, not a mock: a trigger that refuses to delete the chats row, which is the last thing purge_chat() does.
        Nothing deleted before it may stay deleted, or a half-purged chat would be left behind with no way to see what is missing.
        """
        before = (count(two_chats, "messages"), count(two_chats, "message_edits"), count(two_chats, "message_deletions"), count(two_chats, "senders"))
        two_chats.execute(sa.text("CREATE FUNCTION purge_test_refuse() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'refused'; END; $$ LANGUAGE plpgsql"))
        two_chats.execute(sa.text("CREATE TRIGGER purge_test_refuse BEFORE DELETE ON chats FOR EACH ROW EXECUTE FUNCTION purge_test_refuse()"))
        two_chats.commit()
        try:
            with pytest.raises(DBAPIError, match="refused"):
                q.purge_chat(two_chats, 1)
            assert (count(two_chats, "messages"), count(two_chats, "message_edits"), count(two_chats, "message_deletions"), count(two_chats, "senders")) == before
            assert scalar(two_chats, "SELECT COUNT(*) FROM chats WHERE chat_id = 1") == 1
        finally:
            two_chats.rollback()
            two_chats.execute(sa.text("DROP TRIGGER purge_test_refuse ON chats"))
            two_chats.execute(sa.text("DROP FUNCTION purge_test_refuse()"))
            two_chats.commit()

    def test_the_connection_is_usable_after_a_failed_purge(self, two_chats):
        two_chats.execute(sa.text("CREATE FUNCTION purge_test_refuse() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'refused'; END; $$ LANGUAGE plpgsql"))
        two_chats.execute(sa.text("CREATE TRIGGER purge_test_refuse BEFORE DELETE ON chats FOR EACH ROW EXECUTE FUNCTION purge_test_refuse()"))
        two_chats.commit()
        try:
            with pytest.raises(DBAPIError):
                q.purge_chat(two_chats, 1)
            assert scalar(two_chats, "SELECT COUNT(*) FROM chats") == 2  # no "transaction is aborted" error left behind
        finally:
            two_chats.rollback()
            two_chats.execute(sa.text("DROP TRIGGER purge_test_refuse ON chats"))
            two_chats.execute(sa.text("DROP FUNCTION purge_test_refuse()"))
            two_chats.commit()


class TestPurgeWhileTheArchiverIsWriting:
    def test_a_message_being_written_at_that_moment_is_waited_for_not_lost_or_left_behind(self, archive, two_chats):
        """
        The live archiver keeps running during a purge. Simulate it with a second connection that has inserted a message into the chat but not committed yet:
        the purge must WAIT for it (the lock on the chat row), then remove it with the rest - never fail on the foreign key, never leave it behind as an orphan.
        """
        import threading
        import time

        import db

        writer_engine = sa.create_engine(db.server_database_url(archive.name), poolclass=NullPool)
        purge_engine = sa.create_engine(db.server_database_url(archive.name), poolclass=NullPool)
        outcome: dict = {}

        def purge() -> None:
            try:
                with purge_engine.connect() as conn:
                    outcome["result"] = q.purge_chat(conn, 1)
            except Exception as exc:  # noqa: BLE001 - reported to the main thread below
                outcome["error"] = exc

        try:
            with writer_engine.connect() as writer:
                writer.execute(sa.text("INSERT INTO messages (tg_message_id, chat_id, text, date) VALUES (99, 1, 'late arrival', now())"))
                thread = threading.Thread(target=purge)
                thread.start()
                time.sleep(0.7)
                assert thread.is_alive(), "the purge should be waiting for the uncommitted insert, not racing past it"
                writer.commit()
                thread.join(timeout=15)
                assert not thread.is_alive()
        finally:
            writer_engine.dispose()
            purge_engine.dispose()

        assert "error" not in outcome, outcome.get("error")
        assert outcome["result"]["messages_deleted"] == 5  # the four seeded ones plus the late arrival
        assert scalar(two_chats, "SELECT COUNT(*) FROM messages WHERE chat_id = 1") == 0