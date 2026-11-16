"""
utils/retention_rules.py: the pure decisions behind the retention policy's two safeguards (the confirmation and the per-run limit) and its schedule.

Deleting is irreversible, so the edges are pinned exactly: the tolerance is a floor OR a share, the first run is bound by what the owner confirmed,
later runs by a share of the archive, a blocked run stays blocked until the policy is saved again.
"""

from datetime import datetime, timedelta, timezone

import pytest

from utils import retention_rules as rules
from utils.validation import (
    RETENTION_CONFIRM_TOLERANCE_MIN,
    RETENTION_MAX_MONTHS,
    RETENTION_MIN_MONTHS,
    RETENTION_RUN_INTERVAL_HOURS,
    RETENTION_STEADY_RUN_MIN,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


class TestLimits:
    def test_the_minimum_is_one_month_and_the_maximum_ten_years(self):
        assert (RETENTION_MIN_MONTHS, RETENTION_MAX_MONTHS) == (1, 120)


class TestTolerance:
    def test_a_small_confirmation_gets_the_floor(self):
        assert rules.confirmation_tolerance(0) == RETENTION_CONFIRM_TOLERANCE_MIN
        assert rules.confirmation_tolerance(500) == RETENTION_CONFIRM_TOLERANCE_MIN

    def test_a_large_confirmation_gets_a_tenth(self):
        assert rules.confirmation_tolerance(100_000) == 10_000

    def test_the_floor_and_the_share_meet_at_ten_thousand(self):
        assert rules.confirmation_tolerance(10_000) == 1000
        assert rules.confirmation_tolerance(10_010) == 1001


class TestPreviewIsStale:
    @pytest.mark.parametrize("confirmed, actual, stale", [
        (0, 0, False),
        (0, 1000, False),  # exactly at the tolerance
        (0, 1001, True),
        (5000, 6000, False),
        (5000, 6001, True),
        (100_000, 110_000, False),
        (100_000, 110_001, True),
        (5000, 10, False),  # FEWER than confirmed is always fine: the owner agreed to more
    ])
    def test_boundaries(self, confirmed, actual, stale):
        assert rules.preview_is_stale(confirmed, actual) is stale


class TestRunLimit:
    def test_the_first_run_is_bound_by_what_was_confirmed_plus_tolerance(self):
        assert rules.run_limit(5000, total_messages=10**9) == 6000
        assert rules.run_limit(0, total_messages=10**9) == 1000

    def test_a_confirmed_run_ignores_the_archive_size(self):
        assert rules.run_limit(5000, total_messages=1) == rules.run_limit(5000, total_messages=10**9)

    def test_a_steady_run_has_a_floor(self):
        assert rules.run_limit(None, total_messages=0) == RETENTION_STEADY_RUN_MIN
        assert rules.run_limit(None, total_messages=50_000) == RETENTION_STEADY_RUN_MIN

    def test_a_steady_run_on_a_big_archive_is_a_tenth_of_it(self):
        assert rules.run_limit(None, total_messages=2_000_000) == 200_000


class TestRunIsDue:
    def due(self, *, last_run_at, status=None, updated_at=NOW - timedelta(days=400), now=NOW):
        return rules.run_is_due(last_run_at=last_run_at, last_run_status=status, updated_at=updated_at, now=now)

    def test_never_run_is_due(self):
        assert self.due(last_run_at=None) is True

    def test_a_run_inside_the_interval_is_not_due(self):
        assert self.due(last_run_at=NOW - timedelta(hours=RETENTION_RUN_INTERVAL_HOURS - 1), status="ok") is False

    def test_a_run_at_the_interval_is_due(self):
        assert self.due(last_run_at=NOW - timedelta(hours=RETENTION_RUN_INTERVAL_HOURS), status="ok") is True

    def test_saving_again_after_the_last_run_is_due_at_once(self):
        last = NOW - timedelta(hours=1)
        assert self.due(last_run_at=last, status="ok", updated_at=last + timedelta(seconds=1)) is True

    def test_a_blocked_run_stays_blocked_however_long_it_waits(self):
        last = NOW - timedelta(days=90)
        assert self.due(last_run_at=last, status="blocked", updated_at=last - timedelta(days=1)) is False

    def test_saving_releases_a_blocked_run(self):
        last = NOW - timedelta(days=2)
        assert self.due(last_run_at=last, status="blocked", updated_at=last + timedelta(minutes=5)) is True

    def test_an_error_is_retried_at_the_normal_pace_not_at_once(self):
        assert self.due(last_run_at=NOW - timedelta(minutes=5), status="error") is False
        assert self.due(last_run_at=NOW - timedelta(hours=RETENTION_RUN_INTERVAL_HOURS), status="error") is True
