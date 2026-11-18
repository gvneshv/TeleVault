"""backfill._sleep_unless_cancelled - the sleep every deliberate pause (chat pause, flood wait) goes through."""

import asyncio
import time

import backfill


def test_it_sleeps_the_full_time_when_nothing_cancels(monkeypatch):
    monkeypatch.setattr(backfill, "_cancelled", False)
    start = time.monotonic()
    asyncio.run(backfill._sleep_unless_cancelled(0.15))
    assert time.monotonic() - start >= 0.15


def test_it_ends_at_once_when_the_run_is_already_cancelled(monkeypatch):
    monkeypatch.setattr(backfill, "_cancelled", True)
    start = time.monotonic()
    asyncio.run(backfill._sleep_unless_cancelled(30))  # a flood wait of this size must not hold a cancelled run
    assert time.monotonic() - start < 1


def test_a_cancel_during_the_sleep_ends_it_within_a_second(monkeypatch):
    monkeypatch.setattr(backfill, "_cancelled", False)

    async def scenario():
        task = asyncio.create_task(backfill._sleep_unless_cancelled(30))
        await asyncio.sleep(0.1)
        backfill._cancelled = True
        start = time.monotonic()
        await task
        return time.monotonic() - start

    assert asyncio.run(scenario()) < 1.5
