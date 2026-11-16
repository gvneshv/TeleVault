"""
backfill.py and the retention policy, end to end (real run() against a real archive, only Telegram faked; fixtures from test_backfill_blocklist.py).

A backfill must not re-import messages the policy would delete again at its next run.
The fake Telegram dates every message 2026-01-01, so "older than one month" and "newer than ten years" are both true for as long as this test suite makes sense.
"""

import db.queries as q
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (a fixture imported to be used)


def test_messages_older_than_an_active_policys_cutoff_are_not_imported(archive, telegram):  # noqa: F811
    q.save_retention_policy(archive.conn, enabled=True, months=1, keep_deleted=True, confirmed_messages=0)
    _, status = telegram({11: ["old one", "old two"]})
    assert stored(archive.conn) == {}
    assert status["state"] == "completed"


def test_messages_newer_than_the_cutoff_are_imported_as_usual(archive, telegram):  # noqa: F811
    q.save_retention_policy(archive.conn, enabled=True, months=120, keep_deleted=True, confirmed_messages=0)
    telegram({11: ["fine"]})
    assert stored(archive.conn) == {11: 1}


def test_a_switched_off_policy_changes_nothing(archive, telegram):  # noqa: F811
    q.save_retention_policy(archive.conn, enabled=False, months=1, keep_deleted=True, confirmed_messages=None)
    telegram({11: ["kept"]})
    assert stored(archive.conn) == {11: 1}
