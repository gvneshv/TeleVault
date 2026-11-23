"""
How gently backfill.py talks to Telegram, end to end against a real archive (only Telegram is faked, see test_backfill_blocklist.py):
pauses between pages and between chats, flood waits of every kind, and a Cancel that really stops.

Every deliberate pause is recorded instead of slept (`telegram.sleeps`), so none of this waits.
"""

import pytest
from telethon import utils

import backfill
from tests.integration.test_backfill_blocklist import _channel, stored, telegram  # noqa: F401  (shared fixture and helpers)
import db.queries as q


def flood(chat_id, *seconds):
    """A `setup` hook injecting flood waits into one chat's history requests."""
    return lambda fake: fake.flood_on_history.update({chat_id: list(seconds)})


class TestPagePacing:
    def test_history_is_requested_with_an_explicit_pause_between_pages(self, archive, telegram):  # noqa: F811
        fake, _ = telegram({11: ["a"]})
        assert fake.iter_calls == [{"chat": 11, "min_id": 0, "wait_time": backfill.PAGE_WAIT_SECONDS}]
        assert 1 <= backfill.PAGE_WAIT_SECONDS <= 2


class TestPauseBetweenChats:
    def test_a_random_pause_separates_chats_that_were_fetched(self, archive, telegram):  # noqa: F811
        telegram({11: ["a"], 12: ["b"], 13: ["c"], 14: ["d"]})
        pauses = telegram.sleeps
        assert len(pauses) == 3  # between four chats - none before the first, none after the last
        assert all(2.0 <= p <= 5.0 for p in pauses)
        assert len(set(pauses)) > 1  # random, not a fixed interval

    def test_a_single_chat_is_never_followed_by_a_pause(self, archive, telegram):  # noqa: F811
        telegram({11: ["a"]})
        assert telegram.sleeps == []

    def test_blocked_chats_cost_no_pause(self, archive, telegram):  # noqa: F811
        q.block_chat(archive.conn, 11, "chat11")
        q.block_chat(archive.conn, 12, "chat12")
        telegram({11: ["a"], 12: ["b"], 13: ["c"]})
        assert telegram.sleeps == []

    def test_the_pause_is_taken_from_the_documented_range(self, monkeypatch):
        seen = []
        monkeypatch.setattr(backfill.random, "uniform", lambda lo, hi: seen.append((lo, hi)) or 3.0)
        slept = []

        async def record(seconds):
            slept.append(seconds)

        monkeypatch.setattr(backfill, "_sleep_unless_cancelled", record)
        import asyncio
        asyncio.run(backfill._pause_between_chats())
        assert seen == [(2.0, 5.0)] and slept == [3.0]


class TestFloodWaits:
    def test_a_long_wait_in_a_chat_is_waited_out_then_retried(self, archive, telegram):  # noqa: F811
        fake, status = telegram({11: ["a", "b"]}, setup=flood(11, 300))
        assert 301 in telegram.sleeps  # asked 300, plus a second
        assert len(fake.iter_calls) == 2 and stored(archive.conn) == {11: 2}
        assert status["state"] == "completed"

    def test_the_web_ui_is_told_while_the_run_waits_and_not_afterwards(self, archive, telegram):  # noqa: F811
        _, status = telegram({11: ["a"]}, setup=flood(11, 300))
        during = [s for s, wait in zip(telegram.snapshots, telegram.sleeps) if wait == 301]
        assert during and during[0]["flood_wait_seconds"] == 300 and during[0]["state"] == "running"
        assert "flood_wait_seconds" not in status

    def test_a_chat_that_keeps_being_throttled_is_given_up_on_and_the_run_moves_on(self, archive, telegram):  # noqa: F811
        fake, status = telegram({11: ["a"], 12: ["b"]}, setup=flood(11, *[10] * backfill.MAX_FLOOD_RETRIES))
        waits = [s for s in telegram.sleeps if s == 11]
        assert len(waits) == backfill.MAX_FLOOD_RETRIES - 1  # nothing is slept after the last attempt - there is no retry left to wait for
        assert stored(archive.conn) == {12: 1}
        # Unfinished work, so not "completed": a completed run starts the backfill cooldown, which would block the retry the log message recommends.
        assert status["state"] == "error"

    def test_a_wait_over_an_hour_stops_the_run_without_sleeping(self, archive, telegram):  # noqa: F811
        too_long = backfill.MAX_FLOOD_WAIT_SECONDS + 1
        fake, status = telegram({11: ["a"], 12: ["b"], 13: ["c"]}, setup=flood(12, too_long))
        assert fake.fetched == [11, 12]  # chat 13 was never requested: the account is throttled, pressing on would make it worse
        assert too_long not in telegram.sleeps and all(s <= 5.0 for s in telegram.sleeps)  # only the pause between chats
        assert stored(archive.conn) == {11: 1}  # what was done stays
        assert status["state"] == "error"

    def test_a_flood_wait_while_reading_the_chat_list_is_retried(self, archive, telegram):  # noqa: F811
        _, status = telegram({11: ["a"]}, setup=lambda f: f.flood_on_dialogs.append(120))
        assert 121 in telegram.sleeps and stored(archive.conn) == {11: 1} and status["state"] == "completed"

    def test_a_too_long_wait_for_the_chat_list_ends_the_run(self, archive, telegram):  # noqa: F811
        fake, status = telegram({11: ["a"]}, setup=lambda f: f.flood_on_dialogs.append(7200))
        assert fake.fetched == [] and status["state"] == "error"

    def test_a_flood_wait_while_looking_up_a_named_chat_is_retried(self, archive, telegram):  # noqa: F811
        _, status = telegram({11: ["a"]}, chat_selector="11", setup=lambda f: f.flood_on_entity.append(90))
        assert 91 in telegram.sleeps and stored(archive.conn) == {11: 1} and status["state"] == "completed"

    def test_a_flood_wait_in_the_migration_probe_is_waited_out_not_swallowed(self, archive, telegram, caplog):  # noqa: F811
        """Regression: the probe's `except Exception` used to log a traceback and carry on, straight into the next request."""
        fake, status = telegram({5: ["a", "b"]}, channels=frozenset({5}), setup=lambda f: f.flood_on_probe.append(200))
        assert 201 in telegram.sleeps
        assert stored(archive.conn) == {utils.get_peer_id(_channel(5, "c")): 2}
        assert "Could not check migration status" not in caplog.text and "Traceback" not in caplog.text


class TestCancel:
    def test_after_a_cancel_no_further_chat_is_requested(self, archive, telegram):  # noqa: F811
        """Regression: every remaining chat used to send one more request just to notice the flag."""
        def cancel(chat_id):
            backfill._cancelled = True

        fake, status = telegram({11: ["a", "b"], 12: ["c"], 13: ["d"]}, setup=lambda f: setattr(f, "on_chat_done", cancel))
        assert fake.fetched == [11]
        assert status["state"] == "cancelled"
        assert stored(archive.conn) == {11: 2}

    def test_a_cancel_arriving_during_the_pause_between_chats_still_stops_before_the_next_chat(self, archive, telegram, monkeypatch):  # noqa: F811
        async def cancelling_sleep(seconds):
            backfill._cancelled = True

        monkeypatch.setattr(backfill, "_sleep_unless_cancelled", cancelling_sleep)
        fake, status = telegram({11: ["a"], 12: ["b"]})
        assert fake.fetched == [11] and status["state"] == "cancelled"
