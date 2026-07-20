"""
One-off script: apply every recorded chat_migrations mapping to existing rows.

Run this AFTER a chat_migrations row exists for a pair you want merged
(populated by either on_message.py's live detection or backfill.py's migrated_from_chat_id check).
Safe to run repeatedly - already-merged chats have no rows left under the old id, so re-running is a no-op for them.
"""
import logging
import sqlite3

from config import settings
from utils.logging_setup import setup_logging

from db import queries

logger = logging.getLogger(__name__)


def merge_all(conn: sqlite3.Connection) -> None:
    migrations = conn.execute("SELECT old_chat_id, new_chat_id FROM chat_migrations").fetchall()
    for old_id, new_id in migrations:
        queries.merge_chat(conn, old_id, new_id)


def main() -> None:
    setup_logging(log_level=settings.log_level, log_file=settings.log_file)
    conn = sqlite3.connect(settings.db_path)
    merge_all(conn)
    conn.close()


if __name__ == "__main__":
    main()