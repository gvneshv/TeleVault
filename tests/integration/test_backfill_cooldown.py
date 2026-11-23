"""
The backfill cooldown: one backfill per period, counted from the end of the last COMPLETED run, for every chat.

What these tests pin: the arithmetic (and that the database's clock does it);
which runs count (only completed ones - a cancelled, flood-stopped or failed run is "the same run, resumed");
that backfill.py itself refuses without touching Telegram or leaving a trace; and that 0 switches it off.
"""

import dataclasses

import pytest
import sqlalchemy as sa

import backfill
import db.queries as q
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (shared fixture and helpers)
from tests.integration.test_backfill_pacing import flood

DAY = 24 * 3600


def add_run(conn, status, finished_ago=None):
    """A backfill_runs row; `finished_ago` is seconds before now (None = still running, no end)."""
    conn.execute(
        sa.text(
            "INSERT INTO backfill_runs (status, finished_at) VALUES "
            "(:status, CASE WHEN CAST(:ago AS integer) IS NULL THEN NULL ELSE now() - make_interval(secs => CAST(:ago AS integer)) END)"
        ),
        {"status": status, "ago": finished_ago},
    )
    conn.commit()


def remaining(conn, cooldown=DAY):
    return q.backfill_cooldown_remaining(conn, cooldown)


def runs(conn):
    conn.rollback()
    return conn.execute(sa.text("SELECT status FROM backfill_runs ORDER BY id")).scalars().all()


class TestRemaining:
    def test_no_run_yet_means_allowed(self, archive):
        assert remaining(archive.conn) == 0

    def test_just_after_a_completed_run_the_whole_period_is_left(self, archive):
        add_run(archive.conn, "completed", finished_ago=0)
        assert DAY - 5 <= remaining(archive.conn) <= DAY

    def test_the_time_already_passed_is_taken_off(self, archive):
        add_run(archive.conn, "completed", finished_ago=DAY - 600)
        assert 595 <= remaining(archive.conn) <= 600

    def test_once_the_period_is_over_it_is_allowed_again(self, archive):
        add_run(archive.conn, "completed", finished_ago=DAY + 1)
        assert remaining(archive.conn) == 0

    def test_the_latest_completed_run_decides(self, archive):
        add_run(archive.conn, "completed", finished_ago=5 * DAY)
        add_run(archive.conn, "completed", finished_ago=DAY - 100)
        assert 95 <= remaining(archive.conn) <= 100

    @pytest.mark.parametrize("status", ["cancelled", "error", "running"])
    def test_a_run_that_did_not_complete_does_not_start_it(self, archive, status):
        add_run(archive.conn, status, finished_ago=0 if status != "running" else None)
        assert remaining(archive.conn) == 0

    def test_a_cancelled_run_after_a_completed_one_does_not_extend_it(self, archive):
        add_run(archive.conn, "completed", finished_ago=DAY - 600)
        add_run(archive.conn, "cancelled", finished_ago=0)
        assert 595 <= remaining(archive.conn) <= 600

    def test_zero_switches_it_off(self, archive):
        add_run(archive.conn, "completed", finished_ago=0)
        assert remaining(archive.conn, cooldown=0) == 0


@pytest.fixture
def cooldown(monkeypatch):
    """Switch the cooldown on (the test environment sets it to 0 so unrelated tests may run back to back)."""
    monkeypatch.setattr(backfill, "settings", dataclasses.replace(backfill.settings, backfill_cooldown_seconds=DAY))


class TestTheRunRefusesItself:
    def test_a_second_backfill_within_the_period_is_refused_before_anything_happens(self, archive, telegram, cooldown, caplog):  # noqa: F811
        add_run(archive.conn, "completed", finished_ago=3600)
        with pytest.raises(SystemExit) as exc:
            telegram({11: ["a", "b"]})
        assert exc.value.code == 1
        assert stored(archive.conn) == {}  # nothing fetched, nothing stored
        assert runs(archive.conn) == ["completed"]  # and no new backfill_runs row: the refusal leaves no trace as a "run"
        assert "BACKFILL_COOLDOWN_HOURS" in caplog.text

    def test_it_also_holds_for_a_single_chat(self, archive, telegram, cooldown):  # noqa: F811
        add_run(archive.conn, "completed", finished_ago=3600)
        with pytest.raises(SystemExit):
            telegram({11: ["a"], 12: ["b"]}, chat_selector="12")
        assert stored(archive.conn) == {}

    def test_after_the_period_it_runs_and_starts_the_next_period(self, archive, telegram, cooldown):  # noqa: F811
        add_run(archive.conn, "completed", finished_ago=DAY + 60)
        _, status = telegram({11: ["a"]})
        assert status["state"] == "completed" and stored(archive.conn) == {11: 1}
        assert remaining(archive.conn) > DAY - 60  # the run just finished: a new period began
        with pytest.raises(SystemExit):
            telegram({11: ["a", "b"]})

    def test_a_cancelled_run_can_be_resumed_at_once(self, archive, telegram, cooldown):  # noqa: F811
        add_run(archive.conn, "cancelled", finished_ago=10)
        _, status = telegram({11: ["a"]})
        assert status["state"] == "completed"

    def test_a_flood_stopped_run_can_be_resumed_at_once(self, archive, telegram, cooldown):  # noqa: F811
        """The point of "a run paused by a flood wait and resumed counts as ONE run"."""
        _, status = telegram({11: ["a"]}, setup=flood(11, backfill.MAX_FLOOD_WAIT_SECONDS + 1))
        assert status["state"] == "error"
        _, status = telegram({11: ["a"]})
        assert status["state"] == "completed"

    def test_a_chat_given_up_on_after_repeated_flood_waits_does_not_start_the_period(self, archive, telegram, cooldown):  # noqa: F811
        """The log says "try again later, perhaps with --chat X": a cooldown after that run would make the advice impossible to follow."""
        _, status = telegram({11: ["a"], 12: ["b"]}, setup=flood(11, *[10] * backfill.MAX_FLOOD_RETRIES))
        assert status["state"] == "error"
        assert remaining(archive.conn) == 0
        _, status = telegram({11: ["a"], 12: ["b"]})
        assert status["state"] == "completed"

    def test_with_the_cooldown_off_runs_follow_each_other(self, archive, telegram):  # noqa: F811
        add_run(archive.conn, "completed", finished_ago=0)
        _, status = telegram({11: ["a"]})  # the test environment has BACKFILL_COOLDOWN_HOURS=0
        assert status["state"] == "completed"
