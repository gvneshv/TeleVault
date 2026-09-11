"""
One-time data migration: copies an existing SQLite TeleVault archive into the new PostgreSQL database.

Run this ONCE, after `alembic upgrade head` has created the (empty) Postgres schema,
and BEFORE main.py or backfill.py are run against Postgres for the first time.
Safe to interrupt and re-run - see "Resumability" below.

Usage:
    python scripts/migrate_sqlite_to_postgres.py            # migrate
    python scripts/migrate_sqlite_to_postgres.py --dry-run  # preview only, writes nothing
    python scripts/migrate_sqlite_to_postgres.py --verify   # compare row counts only, migrates nothing

Source: settings.db_path (the old SQLite file, opened read-only - this script never writes to it).
Target: settings.database_url (must already have the Alembic baseline migration applied - this script does not create tables).

What gets migrated, and in what order:
    1. chat_migrations - standalone, no FK dependencies either direction.
    2. senders - loaded once into memory (small table), upserted lazily as each chat's messages reference them
       (a sender can appear across many chats, so this isn't naturally chat-scoped the way messages are).
    3. chats + messages + message_edits + message_deletions - one chat at a time,
       each chat fully processed inside ONE Postgres transaction (see "Resumability" below for why).
    4. backfill_runs - standalone, migrated last.

Resumability:
    Interrupting this script (Ctrl-C, crash, connection drop) and re-running it from the top is always safe.
    Per chat, EVERYTHING for that chat (its chats row, every message, every edit, every deletion) is written inside one Postgres transaction,
    committed only at the very end of that chat's processing.
    This makes "does this chat_id already exist in the target chats table" a fully reliable signal that ALL of that chat's data was migrated successfully
    - if the transaction never committed, the chats row wouldn't be there either, so a half-done chat is never mistaken for a finished one.
    Already-finished chats are skipped immediately without re-reading their messages from SQLite at all.
    senders and chat_migrations use ON CONFLICT DO NOTHING (idempotent by their natural primary keys, regardless of run history).
    backfill_runs has no natural key to de-duplicate against - re-running that specific step after it already succeeded would create duplicate rows,
    so it's skipped instead whenever the target's row count already matches the source's - low-stakes if this heuristic is ever wrong,
    since nothing in the app actually depends on backfill_runs being correct going forward, unlike the message archive itself.

Datetime handling - the single most important correctness detail in this script:
    The source SQLite database stores datetimes in TWO DIFFERENT STRING FORMATS depending on the column,
    verified against the actual pre-migration code (db/connection.py's _adapt_datetime() and every write path in db/queries.py / backfill.py) rather than assumed:

      - Columns the application always set explicitly in Python
        (messages.date/archived_at/edited_at/deleted_at, message_edits.edited_at, message_deletions.deleted_at):
        an offset-aware ISO 8601 string produced by dt.isoformat() on a timezone-aware (local time) datetime, e.g. '2026-01-01T12:00:00+02:00'.
      - Columns that always fell back to SQLite's own DEFAULT CURRENT_TIMESTAMP because the application never set them explicitly
        (chats.first_seen, senders.first_seen, chat_migrations.migrated_at, backfill_runs.started_at/finished_at):
        SQLite's own format, 'YYYY-MM-DD HH:MM:SS', no offset - documented by SQLite as always being UTC.

    _parse_datetime() below handles both, trying the offset-aware format first and falling back to the naive-UTC one
    - deliberately not assuming which format a given column "should" have based on the categorisation above,
    since that categorisation describes the CURRENT code's behaviour,
    not necessarily every historical version of it that ever wrote to this database over its lifetime.
    Getting this wrong would silently shift real historical timestamps - this script tries both rather than guessing from context.
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text as sql_text

# This script lives in scripts/, one level below the repo root where db/,
# config.py, etc. live - add the repo root to sys.path explicitly so these imports resolve regardless of the working directory it's invoked from
# (same fix alembic/env.py needed, for the same reason).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datetime parsing - see this module's docstring for the full rationale.
# ---------------------------------------------------------------------------


def _parse_datetime(s: str | None) -> datetime | None:
    """
    Parse a datetime string from the source SQLite database into a timezone-aware Python datetime,
    trying both formats known to appear in this database (see module docstring) rather than assuming which one a given column "should" have.
    """
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Source connection
# ---------------------------------------------------------------------------


def _open_source() -> sqlite3.Connection:
    """
    Open the source SQLite database read-only - this script must never write to it, however things go on the Postgres side.
    """
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# chat_migrations
# ---------------------------------------------------------------------------


def migrate_chat_migrations(source: sqlite3.Connection, target, dry_run: bool) -> int:
    rows = source.execute(
        "SELECT old_chat_id, new_chat_id, migrated_at FROM chat_migrations"
    ).fetchall()
    if dry_run:
        logger.info(f"[dry-run] Would migrate {len(rows)} chat_migrations row(s).")
        return len(rows)

    count = 0
    for row in rows:
        result = target.execute(
            sql_text(
                "INSERT INTO chat_migrations (old_chat_id, new_chat_id, migrated_at) "
                "VALUES (:old_chat_id, :new_chat_id, :migrated_at) "
                "ON CONFLICT (old_chat_id) DO NOTHING"
            ),
            {
                "old_chat_id": row["old_chat_id"],
                "new_chat_id": row["new_chat_id"],
                "migrated_at": _parse_datetime(row["migrated_at"]),
            },
        )
        count += result.rowcount
    target.commit()
    logger.info(
        f"Migrated {count} of {len(rows)} chat_migrations row(s) ({len(rows) - count} already present)."
    )
    return len(rows)


# ---------------------------------------------------------------------------
# senders
# ---------------------------------------------------------------------------


def load_source_senders(source: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """
    Load every sender from the source into memory, keyed by sender_id.
    senders is small (one row per distinct Telegram user ever seen, not per message),
    so this is cheap - a few thousand rows at most even for a very active account, unlike messages.
    """
    rows = source.execute("SELECT * FROM senders").fetchall()
    return {row["sender_id"]: row for row in rows}


def ensure_sender_migrated(target, sender_row: sqlite3.Row, dry_run: bool) -> None:
    """
    Upsert one sender, preserving its original first_seen.
    ON CONFLICT DO NOTHING makes this safe to call repeatedly for the same sender_id across multiple chats
    (senders aren't scoped to one chat) without needing to track which senders have already been inserted this run.
    """
    if dry_run:
        return
    target.execute(
        sql_text(
            "INSERT INTO senders (sender_id, username, first_name, last_name, first_seen) "
            "VALUES (:sender_id, :username, :first_name, :last_name, :first_seen) "
            "ON CONFLICT (sender_id) DO NOTHING"
        ),
        {
            "sender_id": sender_row["sender_id"],
            "username": sender_row["username"],
            "first_name": sender_row["first_name"],
            "last_name": sender_row["last_name"],
            "first_seen": _parse_datetime(sender_row["first_seen"]),
        },
    )


# ---------------------------------------------------------------------------
# chats + messages + message_edits + message_deletions
# ---------------------------------------------------------------------------


def _fetch_by_message_ids(source: sqlite3.Connection, table: str, message_ids: tuple[int, ...], batch_size: int = 500) -> list[sqlite3.Row]:
    """
    Fetch all rows from `table` (message_edits or message_deletions) whose message_id is in message_ids,
    batching the IN clause into groups of batch_size rather than one giant query with a bound parameter per id.

    SQLite caps the number of bound parameters per query (SQLITE_MAX_VARIABLE_NUMBER - 999 on older builds, up to 32766 on newer ones, but never unlimited).
    A single busy chat can easily have more messages than that in one archive,
    so building one IN clause per id for the whole chat isn't safe at scale - this was caught by a real migration run against a large channel,
    not found in testing with the small sample data used to build this script originally.
    """
    rows: list[sqlite3.Row] = []
    for i in range(0, len(message_ids), batch_size):
        batch = message_ids[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))
        rows.extend(
            source.execute(f"SELECT * FROM {table} WHERE message_id IN ({placeholders})", batch).fetchall()
        )
    return rows


def migrate_chat(
    source: sqlite3.Connection,
    target,
    chat_row: sqlite3.Row,
    senders_by_id: dict[int, sqlite3.Row],
    dry_run: bool,
) -> tuple[int, int, int]:
    """
    Migrate one chat and everything under it (messages, edits, deletions) as a single Postgres transaction
    - see this module's docstring for why that transaction boundary is exactly what makes resumability safe.

    Returns (messages_migrated, edits_migrated, deletions_migrated).
    """
    chat_id = chat_row["chat_id"]

    exists = target.execute(
        sql_text("SELECT 1 FROM chats WHERE chat_id = :chat_id"), {"chat_id": chat_id}
    ).first()
    if exists:
        logger.debug(f"Chat {chat_id} already migrated - skipping.")
        return (0, 0, 0)

    messages = source.execute(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,)
    ).fetchall()

    if dry_run:
        edit_count = source.execute(
            "SELECT COUNT(*) FROM message_edits WHERE message_id IN "
            "(SELECT id FROM messages WHERE chat_id = ?)",
            (chat_id,),
        ).fetchone()[0]
        deletion_count = source.execute(
            "SELECT COUNT(*) FROM message_deletions WHERE message_id IN "
            "(SELECT id FROM messages WHERE chat_id = ?)",
            (chat_id,),
        ).fetchone()[0]
        logger.info(
            f"[dry-run] Chat {chat_id} ({chat_row['name']!r}): would migrate "
            f"{len(messages)} message(s), {edit_count} edit(s), {deletion_count} deletion(s)."
        )
        return (len(messages), edit_count, deletion_count)

    try:
        # chats row first - denormalized counters start at their column defaults (0/NULL);
        # the AFTER INSERT trigger on messages (chats_counters_ai - see the Alembic baseline migration)
        # builds them up correctly as each message below is inserted, exactly as it would during normal live operation.
        # No separate backfill logic needed here for message_count/deleted_count/edited_count/ first_message_at/last_message_at/last_message_preview.
        target.execute(
            sql_text(
                "INSERT INTO chats (chat_id, name, username, chat_type, first_seen) "
                "VALUES (:chat_id, :name, :username, :chat_type, :first_seen)"
            ),
            {
                "chat_id": chat_id,
                "name": chat_row["name"],
                "username": chat_row["username"],
                "chat_type": chat_row["chat_type"],
                "first_seen": _parse_datetime(chat_row["first_seen"]),
            },
        )

        id_map: dict[int, int] = (
            {}
        )  # old (SQLite) messages.id -> new (Postgres) messages.id

        for msg in messages:
            if msg["sender_id"] is not None:
                sender_row = senders_by_id.get(msg["sender_id"])
                if sender_row is not None:
                    ensure_sender_migrated(target, sender_row, dry_run=False)
                else:
                    # Referenced by a message but absent from senders - shouldn't happen given the FK that existed in the source schema too,
                    # but if it ever does, log it loudly rather than silently dropping the sender_id and breaking the new FK constraint.
                    logger.warning(
                        f"Message {msg['id']} in chat {chat_id} references sender "
                        f"{msg['sender_id']}, which has no row in the source senders table."
                    )

            new_id = target.execute(
                sql_text(
                    "INSERT INTO messages "
                    "(tg_message_id, chat_id, sender_id, text, date, is_edited, edited_at, "
                    "is_deleted, deleted_at, archived_at) "
                    "VALUES (:tg_message_id, :chat_id, :sender_id, :text, :date, :is_edited, "
                    ":edited_at, :is_deleted, :deleted_at, :archived_at) "
                    "RETURNING id"
                ),
                {
                    "tg_message_id": msg["tg_message_id"],
                    "chat_id": chat_id,
                    "sender_id": msg["sender_id"],
                    "text": msg["text"],
                    "date": _parse_datetime(msg["date"]),
                    "is_edited": bool(msg["is_edited"]),
                    "edited_at": _parse_datetime(msg["edited_at"]),
                    "is_deleted": bool(msg["is_deleted"]),
                    "deleted_at": _parse_datetime(msg["deleted_at"]),
                    "archived_at": _parse_datetime(msg["archived_at"]),
                },
            ).scalar()
            id_map[msg["id"]] = new_id

        edits_migrated = 0
        deletions_migrated = 0

        if id_map:
            old_ids = tuple(id_map.keys())

            edit_rows = _fetch_by_message_ids(source, "message_edits", old_ids)
            for edit in edit_rows:
                target.execute(
                    sql_text(
                        "INSERT INTO message_edits (message_id, old_text, new_text, edited_at) "
                        "VALUES (:message_id, :old_text, :new_text, :edited_at)"
                    ),
                    {
                        "message_id": id_map[edit["message_id"]],
                        "old_text": edit["old_text"],
                        "new_text": edit["new_text"],
                        "edited_at": _parse_datetime(edit["edited_at"]),
                    },
                )
                edits_migrated += 1

            deletion_rows = _fetch_by_message_ids(source, "message_deletions", old_ids)
            for deletion in deletion_rows:
                # deleted_by_inference/inference_confidence didn't exist in every historical version of the source schema
                # (added by SQLite migrations 001-003) - guard with a fallback in case this is ever run against an older export that predates them.
                columns = deletion.keys()
                target.execute(
                    sql_text(
                        "INSERT INTO message_deletions "
                        "(message_id, text_snapshot, deleted_at, deleted_by_inference, inference_confidence) "
                        "VALUES (:message_id, :text_snapshot, :deleted_at, :deleted_by_inference, :inference_confidence)"
                    ),
                    {
                        "message_id": id_map[deletion["message_id"]],
                        "text_snapshot": deletion["text_snapshot"],
                        "deleted_at": _parse_datetime(deletion["deleted_at"]),
                        "deleted_by_inference": (
                            deletion["deleted_by_inference"]
                            if "deleted_by_inference" in columns
                            else "unknown"
                        ),
                        "inference_confidence": (
                            deletion["inference_confidence"]
                            if "inference_confidence" in columns
                            else None
                        ),
                    },
                )
                deletions_migrated += 1

        target.commit()
    except Exception:
        target.rollback()
        logger.exception(
            f"Failed migrating chat {chat_id} - rolled back, safe to re-run."
        )
        raise

    logger.info(
        f"Migrated chat {chat_id} ({chat_row['name']!r}): "
        f"{len(messages)} message(s), {edits_migrated} edit(s), {deletions_migrated} deletion(s)."
    )
    return (len(messages), edits_migrated, deletions_migrated)


# ---------------------------------------------------------------------------
# backfill_runs
# ---------------------------------------------------------------------------


def migrate_backfill_runs(source: sqlite3.Connection, target, dry_run: bool) -> int:
    """
    Migrated last, standalone - pure historical/audit data, nothing in the app depends on it going forward.
    No natural unique key to de-duplicate against post-interruption,
    so this step is skipped entirely (rather than risking duplicates) whenever the target's row count already matches the source's
    - see this module's docstring for why that heuristic is an acceptable tradeoff here specifically, unlike the message archive itself.
    """
    rows = source.execute("SELECT * FROM backfill_runs").fetchall()

    if dry_run:
        logger.info(f"[dry-run] Would migrate {len(rows)} backfill_runs row(s).")
        return len(rows)

    existing_count = target.execute(
        sql_text("SELECT COUNT(*) FROM backfill_runs")
    ).scalar()
    if existing_count >= len(rows):
        logger.info(
            f"backfill_runs: target already has {existing_count} row(s) "
            f"(source has {len(rows)}) - skipping, already migrated."
        )
        return len(rows)

    for row in rows:
        target.execute(
            sql_text(
                "INSERT INTO backfill_runs "
                "(started_at, finished_at, status, chat_selector, chats_total, chats_done, "
                "messages_stored, messages_skipped, error_message) "
                "VALUES (:started_at, :finished_at, :status, :chat_selector, :chats_total, :chats_done, "
                ":messages_stored, :messages_skipped, :error_message)"
            ),
            {
                "started_at": _parse_datetime(row["started_at"]),
                "finished_at": _parse_datetime(row["finished_at"]),
                "status": row["status"],
                "chat_selector": row["chat_selector"],
                "chats_total": row["chats_total"],
                "chats_done": row["chats_done"],
                "messages_stored": row["messages_stored"],
                "messages_skipped": row["messages_skipped"],
                "error_message": row["error_message"],
            },
        )
    target.commit()
    logger.info(f"Migrated {len(rows)} backfill_runs row(s).")
    return len(rows)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(source: sqlite3.Connection, target) -> bool:
    """
    Compare row counts between source and target for every migrated table.
    Not a substitute for the per-chat atomicity guarantees above, but a cheap, independent cross-check that nothing was silently missed.

    Returns True if every table's counts match, False otherwise (logged loudly either way rather than left to be discovered later).
    """
    tables = [
        "chats",
        "senders",
        "messages",
        "message_edits",
        "message_deletions",
        "chat_migrations",
        "backfill_runs",
    ]
    all_match = True
    for table in tables:
        source_count = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        target_count = target.execute(
            sql_text(f"SELECT COUNT(*) FROM {table}")
        ).scalar()
        match = source_count == target_count
        all_match = all_match and match
        level = logger.info if match else logger.error
        level(
            f"{table}: source={source_count} target={target_count} {'OK' if match else 'MISMATCH'}"
        )
    return all_match


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be migrated without writing anything.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only compare row counts between source and target; migrates nothing.",
    )
    args = parser.parse_args()

    db.init_db(settings.database_url)
    source = _open_source()
    target = db.get_connection()

    if args.verify:
        ok = verify(source, target)
        sys.exit(0 if ok else 1)

    start = time.monotonic()

    migrate_chat_migrations(source, target, dry_run=args.dry_run)

    senders_by_id = load_source_senders(source)
    logger.info(f"Loaded {len(senders_by_id)} sender(s) from source into memory.")

    chat_rows = source.execute("SELECT * FROM chats ORDER BY chat_id ASC").fetchall()
    total_messages = total_edits = total_deletions = 0
    for chat_row in chat_rows:
        m, e, d = migrate_chat(
            source, target, chat_row, senders_by_id, dry_run=args.dry_run
        )
        total_messages += m
        total_edits += e
        total_deletions += d

    migrate_backfill_runs(source, target, dry_run=args.dry_run)

    elapsed = time.monotonic() - start
    prefix = "[dry-run] Would migrate" if args.dry_run else "Migrated"
    logger.info(
        f"{prefix} {len(chat_rows)} chat(s), {total_messages} message(s), "
        f"{total_edits} edit(s), {total_deletions} deletion(s) in {elapsed:.1f}s."
    )

    if not args.dry_run:
        logger.info("Running verification...")
        ok = verify(source, target)
        if not ok:
            logger.error(
                "Verification found mismatches - see above. Do not proceed until this is resolved."
            )
            sys.exit(1)
        logger.info(
            "Verification passed - source and target row counts match for every table."
        )

    source.close()
    target.close()
    db.close_db()


if __name__ == "__main__":
    main()