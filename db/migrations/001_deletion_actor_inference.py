"""
Adds `deleted_by_inference` and `inference_confidence` to the`message_deletions` table.

Why ALTER TABLE instead of recreating the table:
    Existing deletion records are preserved. 
    Both columns default to safe sentinel values ('unknown' / NULL) so old rows remain valid.

Run condition:
    This migration is idempotent — it checks for column existence before altering.
    Safe to call on every startup from schema.py.

Background on the inference logic:
    Telegram's API does not expose who deleted a message.
    This migration only adds the columns and their sentinel defaults ('unknown' / NULL) ahead of a planned inference feature — it does NOT itself compute anything,
    and at the time this migration was written, nothing else in the codebase did either.
    handlers/on_delete.py called db.queries.flag_deleted() with no actor-inference logic;
    every row got 'unknown' purely from the column DEFAULT below.
    
    UPDATE: a private/group guess (the original plan) was reviewed and deliberately dropped as unreliable — see migration 002,
    which also replaces this migration's CHECK constraint values ('self'/'other') with the channel-only design that shipped instead.
    This migration is left as-is (history), not edited to match — 002 is the source of truth for current behavior.
"""

import sqlite3
import logging

logger = logging.getLogger(__name__)


def run(conn: sqlite3.Connection) -> None:
    """
    Apply migration 001: deletion actor inference columns.

    Called by db/schema.py during startup after the base DDL runs.
    Safe to call multiple times.
    """
    cursor = conn.execute("PRAGMA table_info(message_deletions)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    altered = False

    if "deleted_by_inference" not in existing_columns:
        conn.execute(
            """
            ALTER TABLE message_deletions
            ADD COLUMN deleted_by_inference TEXT
                CHECK(deleted_by_inference IN ('self', 'other', 'unknown'))
                DEFAULT 'unknown'
            """
        )
        altered = True
        logger.debug("Migration 001: added column deleted_by_inference")

    if "inference_confidence" not in existing_columns:
        conn.execute(
            """
            ALTER TABLE message_deletions
            ADD COLUMN inference_confidence TEXT
            """
        )
        altered = True
        logger.debug("Migration 001: added column inference_confidence")

    if altered:
        conn.commit()
        logger.info("Migration 001 applied: deletion actor inference columns added.")
    else:
        logger.debug("Migration 001: columns already present, skipped.")