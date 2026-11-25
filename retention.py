"""
The retention job: applies the owner's "delete archived messages older than N months" policy.

It runs INSIDE the per-account archiver process (main.py starts `retention_loop` next to the heartbeat), so it works only while the archiver runs.
That also makes it safe against a backfill: the archiver and a backfill are mutually exclusive (both hold the Telegram session),
so the two can never run together, and backfill.py itself skips messages older than the policy's cutoff so it does not re-import what this job removed.

Deleting is irreversible, so the job is deliberately cautious (the numbers live in utils/validation.py, the rules in utils/retention_rules.py):

- It does nothing unless the policy is saved AND enabled, and saving needs a confirmed message count (see the API route).
- It looks whether a run is due once a minute (one single-row read) and runs at most once a day, or at once after the policy is saved again.
- Before deleting it counts what it WOULD delete.
  More than the allowed limit (the confirmed first run, then a share of the archive) means the run is BLOCKED:
  nothing is deleted, the policy shows "blocked", and it waits until the owner looks at a new preview and saves again.
  This is what stops a wrong server clock or a months-long pause from wiping years in one go.
- Deleting happens in small batches (db/queries.purge_expired_messages) and never waits on a row the archiver is writing.
- Logs carry counts only - never a name, a text or a date of a message.
"""

import asyncio
import logging
from datetime import datetime, timezone

import db
import db.queries as queries
import db.read_queries as read_queries
from utils.retention_rules import run_is_due, run_limit
from utils.validation import RETENTION_CHECK_INTERVAL_SECONDS

logger = logging.getLogger(__name__)

# One line, once, when the archive predates the retention migration (the loop checks every minute; it must not log every minute).
_outdated_warned = False


def apply_policy_once(conn, *, now: datetime | None = None) -> str | None:
    """
    Apply the policy if (and only if) a run is due. Returns the run's status ('ok', 'blocked', 'error') or None when nothing was due / nothing is configured.

    Blocking (database work): the loop calls it through asyncio.to_thread(). `now` exists for tests.
    Never raises for a database problem inside the run: that becomes status 'error', recorded when the database allows it.
    """
    global _outdated_warned
    now = now or datetime.now(timezone.utc)

    if not queries.retention_table_exists(conn):
        conn.rollback()  # end the read-only transaction the probe opened
        if not _outdated_warned:
            _outdated_warned = True
            logger.warning(
                "This archive has no retention table yet, so automatic cleanup is off. Run `python scripts/migrate_archives.py` (RUNBOOK.md) and restart the archiver."
            )
        return None

    policy = read_queries.get_retention_policy(conn)
    conn.rollback()
    if policy is None or not policy["enabled"]:
        return None
    if not run_is_due(
        last_run_at=policy["last_run_at"], last_run_status=policy["last_run_status"], updated_at=policy["updated_at"], now=now
    ):
        return None

    months, keep_deleted, confirmed = policy["months"], policy["keep_deleted"], policy["confirmed_messages"]
    try:
        preview = read_queries.count_retention_candidates(conn, months, keep_deleted)
        conn.rollback()
        limit = run_limit(confirmed, preview["total"])
        if preview["count"] > limit:
            queries.record_retention_run(conn, status="blocked", deleted=0, clear_confirmation=False)
            logger.warning(
                "Retention run BLOCKED: it would delete %d messages, more than the allowed %d. Nothing was deleted; open Settings, check the preview and save again.",
                preview["count"], limit,
            )
            return "blocked"
        result = queries.purge_expired_messages(conn, preview["cutoff"], keep_deleted=keep_deleted, limit=limit)
        queries.record_retention_run(conn, status="ok", deleted=result["messages_deleted"], clear_confirmation=True)
        logger.info(
            "Retention run: %d messages, %d edits, %d deletion records, %d senders removed.",
            result["messages_deleted"], result["edits_deleted"], result["deletions_deleted"], result["senders_deleted"],
        )
        return "ok"
    except Exception as exc:
        conn.rollback()
        logger.error("Retention run failed (%s); it will be retried later.", type(exc).__name__)
        try:
            queries.record_retention_run(conn, status="error", deleted=0, clear_confirmation=False)
        except Exception:
            conn.rollback()
        return "error"


def _repair_summaries() -> None:
    """Once per archiver start: heal chat list entries that kept a preview after cleanup emptied them (queries.repair_stale_chat_summaries)."""
    with db.get_connection() as conn:
        fixed = queries.repair_stale_chat_summaries(conn)
    if fixed:
        logger.info("Repaired the list entry of %d emptied chat(s).", fixed)


def _tick() -> None:
    """One pass of the loop on its own connection (blocking)."""
    with db.get_connection() as conn:
        apply_policy_once(conn)


async def retention_loop() -> None:
    """
    Check once a minute whether the policy is due, for as long as the archiver runs.
    Cancelled by main.py on shutdown.
    A database outage (Docker down) only skips a pass: archiving matters more than cleanup.
    """
    try:
        await asyncio.to_thread(_repair_summaries)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Chat list repair skipped (%s).", type(exc).__name__)
    while True:
        try:
            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Retention check skipped (%s).", type(exc).__name__)
        await asyncio.sleep(RETENTION_CHECK_INTERVAL_SECONDS)