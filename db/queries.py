"""
All database write operations for the app, plus the read helpers writes themselves depend on
(get_message() and friends - db/read_queries.py is a separate, API-only read layer with its own, differently-shaped queries).

Design rules followed here:
  - Every function takes a connection as its first argument.
    No global state - callers control which connection is used.
  - All writes use an explicit try/commit/except-rollback pattern, same as before.
    Under SQLite this was mostly a hygiene choice;
    under Postgres it's load-bearing - Postgres aborts the ENTIRE transaction on the first error in it (not just the one failing statement),
    so any function issuing more than one statement per transaction must roll back explicitly on failure,
    or every later use of that same connection fails with an unrelated-looking error until something calls rollback().
    See db/connection.py's get_readonly_connection() docstring for the exact same lesson learned the hard way there.
  - Timestamps are stored in the local system timezone, not UTC.
    _now() returns local time; _localise() converts incoming UTC datetimes (e.g. message.date from Telethon) to local time before storage.
    Exception: chats.first_seen and senders.first_seen use Postgres's DEFAULT now() - they're metadata, not message times,
    so which timezone the default represents doesn't matter the way it would for a message's own date.
  - archived_at in insert_message is passed explicitly so it uses local time rather than falling back to the DEFAULT now().
  - INSERT ... ON CONFLICT DO NOTHING is used where duplicate arrivals are possible
    (e.g. Telegram sometimes re-delivers events on reconnect) - Postgres's equivalent of SQLite's INSERT OR IGNORE.
    The conflict target in each case is whichever column(s) carry the actual real-world identity
    (chat_id, sender_id, or the (tg_message_id, chat_id) pair) - Postgres matches ON CONFLICT against any unique index covering those columns,
    not only a formally-named constraint, so this works against schema.py's plain unique Index objects with no changes needed there.
  - Functions return meaningful values (row ID, bool, fetched row) so callers can log or react without querying again.
    Getting the generated id back now uses INSERT ... RETURNING id instead of sqlite3's cursor.lastrowid, which has no Postgres/psycopg equivalent.
  - Boolean flags in the schema are native BOOLEAN now (were INTEGER 0/1 in SQLite).
    Comparisons and assignments use Python True/False as bound parameters rather than SQL TRUE/FALSE literals or 1/0.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, ProgrammingError

from db.connection import is_undefined_table_error
from utils.text_matching import fold

logger = logging.getLogger(__name__)

# Set once the "archive predates the blocklist table" warning in is_chat_blocked() has been logged, so a busy account does not log it for every message.
_blocklist_table_missing_warned = False
# The ignore-rule tables came with a later migration than the blocklist, so an archive can have the blocklist table and not these.
# They are probed with to_regclass() like the name-history table (see _name_history_ready()), not with "try it and catch undefined_table":
# a failing statement is logged by Postgres as an ERROR for every text message of an un-migrated archive.
# Archives (by database name) known to have the tables, so the probe runs once per process. Only a positive answer is remembered:
# an archive migrated while the archiver runs starts applying rules on the very next message, no restart.
_archives_with_ignore_rules: set[str] = set()
_ignore_rules_table_missing_warned = False


def _ignore_rules_ready(conn: Connection) -> bool:
    """True when this archive has the ignore-rule tables (migration b94e2d7a1c63). Quiet when it has not; once seen, never probed again for that database."""
    name = conn.engine.url.database or ""
    if name in _archives_with_ignore_rules:
        return True
    present = bool(conn.execute(sql_text("SELECT to_regclass('public.ingest_ignore_rules') IS NOT NULL")).scalar())
    if present:
        _archives_with_ignore_rules.add(name)
    return present


def get_last_archived_message_id(conn: Connection, chat_id: int) -> int | None:
    """
    Return the highest tg_message_id already archived for a chat, or None if nothing has been archived for it yet (a brand-new chat).

    Telegram message IDs are monotonically increasing within a chat,
    so this is a safe "high water mark":
    backfill.py passes it as Telethon's min_id to fetch only messages newer than what's already archived,
    instead of re-walking the chat's entire history on every run.
    See backfill.py's module docstring for the full incremental-backfill design.
    """
    row = conn.execute(
        sql_text("SELECT MAX(tg_message_id) FROM messages WHERE chat_id = :chat_id"),
        {"chat_id": chat_id},
    ).first()
    return row[0] if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """
    Current time in the local system timezone.
    """
    return datetime.now().astimezone()


def _localise(dt: datetime) -> datetime:
    """
    Convert any datetime to the local system timezone.

    Naive datetimes are assumed to be UTC (this is what Telethon provides for message.date).
    Timezone-aware datetimes are converted directly.

    Always returns a timezone-aware datetime - important now that the destination column is TIMESTAMPTZ:
    psycopg stores a naive datetime as whatever the session's timezone happens to be rather than raising an error, which would silently store the wrong instant. Every write in this module goes through here or _now() specifically to avoid that.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _commit(conn: Connection) -> None:
    """
    Commit the current transaction. Roll back and re-raise on failure.
    """
    try:
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


def upsert_chat(
    conn: Connection,
    chat_id: int,
    name: str | None,
    chat_type: str,
    username: str | None = None,
    commit: bool = True,
) -> None:
    """
    Insert a chat record, or refresh the name/@handle of one we already have.

    The ``username`` is the @handle - present for public groups and channels, None for private chats and legacy groups without a public link.
    Stored as-is without the leading '@' for cleaner querying.

    Renames: when the chat already exists, ``name`` and ``username`` are updated to the values passed in,
    so a chat renamed in Telegram shows its current name in the archive (old messages included - there is one name per chat, not one per message).
    Two guards keep this from corrupting good data:
      - ``name=None`` means "the caller could not resolve the chat entity" (e.g. a transient lookup failure), NOT "the chat has no name".
        In that case nothing is changed, so a hiccup can never blank out a known name or handle.
      - When ``name`` IS given, ``username`` is taken as-is, including None: a public group turning private really does lose its @handle.
    The update only fires when something actually differs, so repeated events for an unchanged chat cause no writes.
    ``chat_type`` and ``first_seen`` are never changed here (a chat's type is fixed by the schema's CHECK constraint; see merge_chat for supergroup upgrades).

    commit=False
        Skip the commit, leaving the INSERT as part of the caller's ongoing transaction.
        Use this when upsert_chat and upsert_sender are called immediately before insert_message - grouping all three into one transaction lets the FK check in insert_message see the parent rows within the same transaction.

    When commit=True (the default), a RuntimeError is raised if the chat_type value fails the CHECK constraint.
    Note this fires differently than it did under SQLite:
    SQLite's INSERT OR IGNORE silently swallowed ANY constraint violation (so the failure was only detectable via a follow-up existence check);
    Postgres's ON CONFLICT DO NOTHING only suppresses a conflict on the exact target named (chat_id here) - a CHECK violation still raises immediately,
    caught below and translated into the same RuntimeError contract callers already expect.

    Note: first_seen uses Postgres's DEFAULT now().
    This column is metadata about when TeleVault first saw the chat, not a message timestamp, so which timezone the default resolves to is acceptable here.
    """
    chat_id = resolve_chat_id(conn, chat_id)

    try:
        conn.execute(
            sql_text(
                "INSERT INTO chats (chat_id, name, username, chat_type) "
                "VALUES (:chat_id, :name, :username, :chat_type) "
                "ON CONFLICT (chat_id) DO UPDATE SET name = EXCLUDED.name, username = EXCLUDED.username "
                "WHERE EXCLUDED.name IS NOT NULL "
                "AND (chats.name IS DISTINCT FROM EXCLUDED.name OR chats.username IS DISTINCT FROM EXCLUDED.username)"
            ),
            {
                "chat_id": chat_id,
                "name": name,
                "username": username,
                "chat_type": chat_type,
            },
        )
    except IntegrityError as e:
        conn.rollback()
        raise RuntimeError(
            f"upsert_chat: failed to insert chat_id={chat_id!r} "
            f"(chat_type={chat_type!r}, name={name!r}). "
            f"The chat_type value likely failed the CHECK constraint. "
            f"Valid values: 'private', 'group', 'supergroup', 'channel'."
        ) from e
    except Exception:
        conn.rollback()
        raise

    if not commit:
        return

    try:
        _commit(conn)
    except Exception:
        conn.rollback()
        raise

    # Defensive fallback only - the CHECK-violation case above is now caught (and translated) before this point is ever reached.
    # Kept in case some other, currently-unanticipated condition causes ON CONFLICT DO NOTHING to skip the row for a reason that doesn't raise.
    exists = conn.execute(
        sql_text("SELECT 1 FROM chats WHERE chat_id = :chat_id"), {"chat_id": chat_id}
    ).first()
    if not exists:
        raise RuntimeError(
            f"upsert_chat: chat_id={chat_id!r} is still absent after insert "
            f"with no exception raised - unexpected. (chat_type={chat_type!r}, name={name!r})"
        )


def merge_chat(conn: Connection, old_chat_id: int, new_chat_id: int) -> None:
    """
    Move all messages from old_chat_id to new_chat_id, then remove the old chats row.

    Rows that collide with an existing (tg_message_id, chat_id) row under new_chat_id - the same message archived twice under both IDs before this mapping existed
    - are deleted outright: backfilled rows never carry richer edit/deletion history than the surviving row already has, so there's nothing worth keeping separately.

    The bulk UPDATE below moves every row EXCEPT ones that would collide
    (a NOT EXISTS guard, checked before the move) - the Postgres equivalent of SQLite's UPDATE OR IGNORE, which has no direct Postgres counterpart.
    SQLite's OR IGNORE silently skipped just the colliding rows within one statement;
    NOT EXISTS achieves the identical end state
    (colliding rows are left behind under old_chat_id, to be caught and deleted by the leftover cleanup right below) without needing a per-row loop.

    Called automatically by record_chat_migration() right after a migration is recorded, so most migrations are cleaned up within moments of detection.
    Also safe to call directly (e.g. from merge_migrated_chats.py) to retry a mapping that didn't fully merge the first time - it's idempotent.
    """
    try:
        cursor = conn.execute(
            sql_text("""
                UPDATE messages m SET chat_id = :new_chat_id
                WHERE m.chat_id = :old_chat_id
                AND NOT EXISTS (
                    SELECT 1 FROM messages m2
                    WHERE m2.chat_id = :new_chat_id AND m2.tg_message_id = m.tg_message_id
                )
                """),
            {"new_chat_id": new_chat_id, "old_chat_id": old_chat_id},
        )
        moved = cursor.rowcount
        conn.commit()

        leftover = conn.execute(
            sql_text("SELECT id FROM messages WHERE chat_id = :old_chat_id"),
            {"old_chat_id": old_chat_id},
        ).fetchall()
        for (msg_id,) in leftover:
            conn.execute(
                sql_text("DELETE FROM message_edits WHERE message_id = :id"),
                {"id": msg_id},
            )
            conn.execute(
                sql_text("DELETE FROM message_deletions WHERE message_id = :id"),
                {"id": msg_id},
            )
            conn.execute(
                sql_text("DELETE FROM messages WHERE id = :id"), {"id": msg_id}
            )
        if leftover:
            conn.commit()

        still_remaining = conn.execute(
            sql_text("SELECT COUNT(*) FROM messages WHERE chat_id = :old_chat_id"),
            {"old_chat_id": old_chat_id},
        ).scalar()

        if still_remaining == 0:
            conn.execute(
                sql_text("DELETE FROM chats WHERE chat_id = :old_chat_id"),
                {"old_chat_id": old_chat_id},
            )
            conn.commit()
            logger.info(
                f"Merged chat {old_chat_id} -> {new_chat_id}: moved {moved}, "
                f"removed {len(leftover)} duplicate leftovers, old chat row removed."
            )
        else:
            logger.warning(
                f"Chat {old_chat_id} -> {new_chat_id}: still has {still_remaining} rows "
                f"after cleanup - needs manual review."
            )
    except Exception:
        conn.rollback()
        raise


def record_chat_migration(conn: Connection, old_chat_id: int, new_chat_id: int) -> None:
    """
    Record that old_chat_id has migrated to new_chat_id (basic group -> supergroup upgrade).

    ON CONFLICT DO NOTHING: if this migration was already recorded
    - e.g. both the MessageActionChatMigrateTo and MessageActionChannelMigrateFrom service messages fired for the same event,
    or backfill re-detects it on a later run - this is a no-op.

    Does not touch any existing chats/messages rows.
    resolve_chat_id() applies the mapping at write time going forward only;
    run merge_migrated_chats.py separately to fix up rows already stored under old_chat_id before this mapping existed.
    """
    if old_chat_id == new_chat_id:
        return
    try:
        conn.execute(
            sql_text(
                "INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (:old_chat_id, :new_chat_id) "
                "ON CONFLICT (old_chat_id) DO NOTHING"
            ),
            {"old_chat_id": old_chat_id, "new_chat_id": new_chat_id},
        )
        _commit(conn)
    except Exception:
        conn.rollback()
        raise
    logger.info(f"Recorded chat migration: {old_chat_id} -> {new_chat_id}.")

    # A blocked basic group must stay blocked once it becomes a supergroup - see _move_blocklist_rule().
    _move_blocklist_rule(conn, old_chat_id, new_chat_id)
    # Likewise a message ignore rule limited to that group - see _move_ignore_rule_chats().
    _move_ignore_rule_chats(conn, old_chat_id, new_chat_id)

    # Fold any pre-existing rows under old_chat_id in immediately, rather than waiting for a manual merge_migrated_chats.py run.
    # Safe every time - merge_chat() is a no-op if old_chat_id already has nothing left.
    merge_chat(conn, old_chat_id, new_chat_id)


# ---------------------------------------------------------------------------
# Ingest blocklist (chats the owner chose not to archive)
# ---------------------------------------------------------------------------
# Policy and the "why" live in handlers/ingest_filter.py; the table's shape and the no-foreign-key decision in db/schema.py.
# Every rule is keyed by the CANONICAL chat id (resolve_chat_id()), the same id insert_message() would store the message under -
# so "is this message blocked?" and "which chat would it be written to?" can never disagree, even for a group that has since been upgraded.


def is_chat_blocked(conn: Connection, chat_id: int) -> bool:
    """
    True if messages from `chat_id` must not be archived.

    Called by the live handlers (once per event, BEFORE any Telegram entity lookup or database write) and by backfill.py (once per chat).
    `chat_id` may be an old, pre-migration id: it is canonicalized first, exactly as insert_message() does.

    An archive that predates the blocklist migration has no table. That can only mean no rules exist (nobody could have created one),
    so this answers False - the same behaviour as before the feature existed - and warns ONCE that the archive needs updating (`python scripts/migrate_archives.py`).
    Any OTHER database error propagates: the caller must then skip the message rather than guess (handlers/ingest_filter.py).
    """
    global _blocklist_table_missing_warned
    try:
        canonical = resolve_chat_id(conn, chat_id)
        row = conn.execute(
            sql_text("SELECT 1 FROM ingest_blocked_chats WHERE chat_id = :chat_id"),
            {"chat_id": canonical},
        ).first()
    except ProgrammingError as exc:
        # A failed statement aborts the whole Postgres transaction - release it before this connection is used (or returned to the pool) again.
        conn.rollback()
        if not is_undefined_table_error(exc):
            raise
        if not _blocklist_table_missing_warned:
            _blocklist_table_missing_warned = True
            logger.warning(
                "This archive has no ingest_blocked_chats table yet, so no chat can be blocked. "
                "Run `python scripts/migrate_archives.py` (see RUNBOOK.md) to update it and enable the blocklist."
            )
        return False
    return row is not None


def block_chat(conn: Connection, chat_id: int, name: str | None, commit: bool = True) -> bool:
    """
    Add a chat to the blocklist. Returns True if it was newly blocked, False if it already was (the existing rule, and its name, are left alone).

    `name` is copied into the rule so the list stays readable after the chat's own row is gone - see db/schema.py's comment on the table.
    Only stops FUTURE archiving: messages already stored stay where they are (removing them is a separate, destructive action).
    """
    chat_id = resolve_chat_id(conn, chat_id)
    try:
        row = conn.execute(
            sql_text(
                "INSERT INTO ingest_blocked_chats (chat_id, name) VALUES (:chat_id, :name) "
                "ON CONFLICT (chat_id) DO NOTHING RETURNING chat_id"
            ),
            {"chat_id": chat_id, "name": name},
        ).first()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return row is not None


def unblock_chat(conn: Connection, chat_id: int, commit: bool = True) -> bool:
    """
    Remove a chat from the blocklist. Returns True if a rule was removed, False if the chat was not blocked.
    Archiving resumes from the next message - nothing that arrived while it was blocked can be recovered (it was never stored).
    """
    chat_id = resolve_chat_id(conn, chat_id)
    try:
        row = conn.execute(
            sql_text("DELETE FROM ingest_blocked_chats WHERE chat_id = :chat_id RETURNING chat_id"),
            {"chat_id": chat_id},
        ).first()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return row is not None


# ---------------------------------------------------------------------------
# Message ignore rules (text phrases that keep a message out of the archive)
# ---------------------------------------------------------------------------
# Policy in handlers/ingest_filter.py, matching semantics in utils/text_matching.py, table shape in db/schema.py.


def get_ignore_patterns(conn: Connection, chat_id: int) -> list[str]:
    """
    The folded phrases (ingest_ignore_rules.pattern_key) that apply to messages of `chat_id`: every "all chats" rule plus every rule limited to this chat.

    Called by the live handlers (once per plain-text message) and by backfill.py (once per chat); the caller compares them with utils.text_matching.matches_any().
    `chat_id` may be an old, pre-migration id: it is canonicalized first, exactly as is_chat_blocked() does.
    Returns only keys, never the original text of a rule, and nothing here logs a rule.

    An archive that predates the ignore-rule migration has no tables, which can only mean no rules exist,
    so this answers [] (archiving as before) and warns ONCE that the archive needs updating.
    Any database error on an archive that does have them propagates: the caller must then skip the message rather than guess.
    """
    global _ignore_rules_table_missing_warned
    if not _ignore_rules_ready(conn):
        if not _ignore_rules_table_missing_warned:
            _ignore_rules_table_missing_warned = True
            logger.warning(
                "This archive has no ingest_ignore_rules table yet, so no message can be ignored by a rule. "
                "Run `python scripts/migrate_archives.py` (see RUNBOOK.md) to update it and enable ignore rules."
            )
        return []
    try:
        canonical = resolve_chat_id(conn, chat_id)
        rows = conn.execute(
            sql_text(
                "SELECT r.pattern_key FROM ingest_ignore_rules r "
                "WHERE r.all_chats OR EXISTS ("
                "  SELECT 1 FROM ingest_ignore_rule_chats c WHERE c.rule_id = r.id AND c.chat_id = :chat_id"
                ")"
            ),
            {"chat_id": canonical},
        ).all()
    except Exception:
        # A failed statement aborts the whole Postgres transaction - release it before this connection is used (or returned to the pool) again.
        conn.rollback()
        raise
    return [row[0] for row in rows]


def add_ignore_rule(
    conn: Connection,
    pattern: str,
    all_chats: bool,
    chats: Sequence[tuple[int, str | None]] = (),
    commit: bool = True,
) -> int | None:
    """
    Create a rule and (unless it is an "all chats" rule) the rows that limit it to `chats`, as one transaction. Returns the new rule's id.

    Returns None, writing nothing, if a rule with the same phrase already exists (compared folded: "Тривога" and "тривога" are one rule).
    The caller (api/routes/ingest.py) is responsible for the limits and for passing CANONICAL chat ids (resolve_chat_id()) with their current names;
    an "all chats" rule must come with no chats, a limited one with at least one - checked there, since it is a request error rather than a data error.
    """
    key = fold(pattern)
    try:
        row = conn.execute(
            sql_text(
                "INSERT INTO ingest_ignore_rules (pattern, pattern_key, all_chats) VALUES (:pattern, :key, :all_chats) "
                "ON CONFLICT (pattern_key) DO NOTHING RETURNING id"
            ),
            {"pattern": pattern, "key": key, "all_chats": all_chats},
        ).first()
        if row is None:
            conn.rollback()
            return None
        rule_id = int(row[0])
        if chats:
            conn.execute(
                sql_text("INSERT INTO ingest_ignore_rule_chats (rule_id, chat_id, name) VALUES (:rule_id, :chat_id, :name)"),
                [{"rule_id": rule_id, "chat_id": chat_id, "name": name} for chat_id, name in chats],
            )
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return rule_id


def remove_ignore_rule(conn: Connection, rule_id: int, commit: bool = True) -> bool:
    """Delete a rule (its chat rows go with it, ON DELETE CASCADE).
    Returns False if there was no such rule.
    Messages are archived again from the next one on."""
    try:
        row = conn.execute(
            sql_text("DELETE FROM ingest_ignore_rules WHERE id = :rule_id RETURNING id"),
            {"rule_id": rule_id},
        ).first()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return row is not None


def _move_ignore_rule_chats(conn: Connection, old_chat_id: int, new_chat_id: int) -> None:
    """
    Carry the chat scope of ignore rules over when a basic group becomes a supergroup (record_chat_migration()).

    Same reasoning as _move_blocklist_rule(): the upgrade gives the chat a brand-new id, and a rule limited to the old id would stop applying
    (get_ignore_patterns() canonicalizes to the NEW id before looking), so the announcements it was meant to keep out would start being archived.
    Rows are MOVED, not copied; a rule that already lists the new id keeps that row and the old one is dropped.

    Does nothing on an archive without the tables (see get_ignore_patterns()): there is nothing to move.
    """
    if not _ignore_rules_ready(conn):
        return
    try:
        conn.execute(
            sql_text(
                "INSERT INTO ingest_ignore_rule_chats (rule_id, chat_id, name) "
                "SELECT rule_id, :new_chat_id, name FROM ingest_ignore_rule_chats WHERE chat_id = :old_chat_id "
                "ON CONFLICT (rule_id, chat_id) DO NOTHING"
            ),
            {"old_chat_id": old_chat_id, "new_chat_id": new_chat_id},
        )
        conn.execute(
            sql_text("DELETE FROM ingest_ignore_rule_chats WHERE chat_id = :old_chat_id"),
            {"old_chat_id": old_chat_id},
        )
        _commit(conn)
    except Exception:
        conn.rollback()
        raise


def record_archive_owner(conn: Connection, owner_id: int, commit: bool = True) -> None:
    """
    Remember which Telegram account this archive belongs to (db/schema.py's archive_owner: one row, overwritten).

    Called by the archiver at start-up with the id Telegram reported for the logged-in account.
    It is what lets the web UI tell the owner's messages from everybody else's without any per-message flag (see the table's comment).
    Idempotent: a restart with the same account only refreshes `recorded_at`.

    Raises ProgrammingError (undefined table) on an archive that predates the migration - the caller decides whether that is fatal
    (main.py logs a warning and carries on: archiving matters more than the colour of a bubble).
    """
    try:
        conn.execute(
            sql_text(
                "INSERT INTO archive_owner (singleton, owner_id) VALUES (TRUE, :owner_id) "
                "ON CONFLICT (singleton) DO UPDATE SET owner_id = EXCLUDED.owner_id, recorded_at = now()"
            ),
            {"owner_id": owner_id},
        )
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise


def _move_blocklist_rule(conn: Connection, old_chat_id: int, new_chat_id: int) -> None:
    """
    Carry a blocklist rule over when a basic group becomes a supergroup (record_chat_migration()).

    The upgrade gives the chat a brand-new id. Without this, a chat the person blocked would start being archived again the moment it was upgraded -
    exactly the kind of silent surprise a blocklist exists to prevent.
    The rule is MOVED, not copied: is_chat_blocked() canonicalizes through chat_migrations first,
    so a rule left on the old id would never be looked at again and would only show up as a duplicate in the list.
    If the new id is somehow blocked already, that rule wins and the old one is simply dropped.

    Tolerates an archive without the table (see is_chat_blocked()): nothing to move there.
    """
    try:
        conn.execute(
            sql_text(
                "INSERT INTO ingest_blocked_chats (chat_id, name, blocked_at) "
                "SELECT :new_chat_id, name, blocked_at FROM ingest_blocked_chats WHERE chat_id = :old_chat_id "
                "ON CONFLICT (chat_id) DO NOTHING"
            ),
            {"old_chat_id": old_chat_id, "new_chat_id": new_chat_id},
        )
        conn.execute(
            sql_text("DELETE FROM ingest_blocked_chats WHERE chat_id = :old_chat_id"),
            {"old_chat_id": old_chat_id},
        )
        _commit(conn)
    except ProgrammingError as exc:
        conn.rollback()
        if not is_undefined_table_error(exc):
            raise
    except Exception:
        conn.rollback()
        raise


def resolve_chat_id(conn: Connection, chat_id: int) -> int:
    """
    Canonicalize a chat_id through any recorded migration chain.

    Walks chat_migrations in case a chat migrated more than once (rare, but not assumed to be a single hop).
    Returns chat_id unchanged if nothing is recorded for it.
    """
    seen = {chat_id}
    current = chat_id
    while True:
        row = conn.execute(
            sql_text(
                "SELECT new_chat_id FROM chat_migrations WHERE old_chat_id = :current"
            ),
            {"current": current},
        ).first()
        if row is None:
            return current
        current = row[0]
        if current in seen:
            logger.warning(
                f"Migration cycle detected resolving chat_id {chat_id} - stopping at {current}."
            )
            return current
        seen.add(current)


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------


# Archives (by database name) known to have the sender_name_history table, so the existence probe in _name_history_ready() runs once per process.
# Only a positive answer is remembered: an archive that is migrated while the archiver runs starts recording on the very next message, no restart.
_archives_with_name_history: set[str] = set()


def _name_history_ready(conn: Connection) -> bool:
    """
    True when this archive has the sender_name_history table (migration a3f81c5d7e92).

    Probed with to_regclass() instead of "try it and catch undefined_table" on purpose:
    a failing statement is logged by Postgres as an ERROR for every message of an un-migrated archive, and it would abort the caller's open transaction.
    to_regclass() answers NULL quietly.
    Once the table is seen it is never probed again for that database in this process.
    """
    name = conn.engine.url.database or ""
    if name in _archives_with_name_history:
        return True
    present = conn.execute(sql_text("SELECT to_regclass('public.sender_name_history') IS NOT NULL")).scalar()
    if present:
        _archives_with_name_history.add(name)
    return bool(present)


# One statement does the whole "has this sender's name changed?" check, so it costs a single round trip and is atomic.
#   cur    : the latest KNOWN identity - the newest history row, else the senders row (history rows rank above it).
#   merged : what we now believe: a field we were given replaces the known one, a NULL (not provided) keeps it.
# A row is inserted only if `merged` differs from `cur`; IS DISTINCT FROM treats NULL as a value, so None -> "x" counts as a change and None -> None does not.
# CAST(... AS text) is needed because psycopg sends an untyped NULL parameter that Postgres cannot type inside COALESCE.
_RECORD_NAME_CHANGE_SQL = """
    WITH cur AS (
        SELECT username, first_name, last_name FROM (
            SELECT username, first_name, last_name, 1 AS prio, id AS ord
              FROM sender_name_history WHERE sender_id = :sender_id
            UNION ALL
            SELECT username, first_name, last_name, 0 AS prio, 0 AS ord
              FROM senders WHERE sender_id = :sender_id
        ) AS known
        ORDER BY prio DESC, ord DESC
        LIMIT 1
    ),
    merged AS (
        SELECT COALESCE(CAST(:username AS text), username)     AS username,
               COALESCE(CAST(:first_name AS text), first_name) AS first_name,
               COALESCE(CAST(:last_name AS text), last_name)   AS last_name,
               username AS old_username, first_name AS old_first_name, last_name AS old_last_name
          FROM cur
    )
    INSERT INTO sender_name_history (sender_id, username, first_name, last_name)
    SELECT :sender_id, username, first_name, last_name
      FROM merged
     WHERE (username, first_name, last_name) IS DISTINCT FROM (old_username, old_first_name, old_last_name)
"""


def _record_name_change(conn: Connection, sender_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    """
    Append a sender_name_history row if the sender is now known under a different name.
    Never raises: name history is a bonus and must not cost a message.

    Runs inside a SAVEPOINT so that if the write fails for any reason, only this statement is undone - the caller's transaction
    (typically upsert_chat + upsert_sender + insert_message, all with commit=False) stays alive and the message is still archived.
    Skipped entirely when nothing was provided (nothing to compare) or the archive has not been migrated yet.

    Two writers seeing the same new name at the same instant (live handler + edit handler) can each add the identical row.
    That is harmless (the history list just shows one more equal entry) and not worth a row lock:
    a lock on `senders` here would deadlock against purge_chat(), which holds the chat row and then deletes senders.
    """
    if username is None and first_name is None and last_name is None:
        return
    try:
        if not _name_history_ready(conn):
            return
        with conn.begin_nested():
            conn.execute(
                sql_text(_RECORD_NAME_CHANGE_SQL),
                {"sender_id": sender_id, "username": username, "first_name": first_name, "last_name": last_name},
            )
    except Exception:
        logger.warning("Could not record a name change for sender %s; the message is archived regardless.", sender_id, exc_info=True)


def upsert_sender(
    conn: Connection,
    sender_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    commit: bool = True,
) -> None:
    """
    Insert a sender record if it doesn't exist yet.
    Same rationale as upsert_chat - the `senders` row keeps the FIRST-SEEN identity and is never rewritten.

    If the sender already exists and is now seen under a different username / first name / last name,
    the change is appended to sender_name_history instead (see _record_name_change() and db/schema.py's comment on that table).
    A None argument means "not provided", not "removed": Telegram often sends partial ("min") users without a username,
    so a removed username cannot be told apart from a missing one and is deliberately not tracked.

    Note: for anonymous admin posts in supergroups, Telegram sets the sender to the group itself, so sender_id may be a negative channel ID rather than a user ID.
    These rows end up in the senders table with whatever fields the channel entity exposes (usually just a name/username).
    This is a Telegram protocol behaviour, not a bug.

    commit=False: same semantics as upsert_chat - see its docstring.
    When commit=True, a RuntimeError is raised if the row is absent after insert with no exception - unexpected,
    since senders has no CHECK constraint (unlike chats) that could cause a legitimate silent rejection.
    """
    try:
        inserted = conn.execute(
            sql_text(
                "INSERT INTO senders (sender_id, username, first_name, last_name) "
                "VALUES (:sender_id, :username, :first_name, :last_name) "
                "ON CONFLICT (sender_id) DO NOTHING RETURNING sender_id"
            ),
            {
                "sender_id": sender_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
            },
        ).first() is not None
    except Exception:
        # No CHECK constraint on senders to translate into a friendlier message the way upsert_chat does - but any error here still aborts the Postgres transaction,
        # so it must still be rolled back before re-raising (see this module's docstring).
        conn.rollback()
        raise

    if not inserted:
        # A brand-new sender has nothing to compare against - its row IS the first identity.
        # Only a known sender can have changed.
        _record_name_change(conn, sender_id, username, first_name, last_name)

    if not commit:
        return

    try:
        _commit(conn)
    except Exception:
        conn.rollback()
        raise

    exists = conn.execute(
        sql_text("SELECT 1 FROM senders WHERE sender_id = :sender_id"),
        {"sender_id": sender_id},
    ).first()
    if not exists:
        raise RuntimeError(
            f"upsert_sender: failed to insert sender_id={sender_id!r} "
            f"(username={username!r}, first_name={first_name!r}, last_name={last_name!r}). "
            f"ON CONFLICT DO NOTHING silently rejected the row - the sender_id value "
            f"likely failed the CHECK constraint."
        )


def set_sender_display_name(conn: Connection, sender_id: int, display_name: str, commit: bool = True) -> dict[str, Any]:
    """
    Give a sender the owner's own label, replacing any earlier one.
    Returns {sender_id, display_name, updated_at}.

    The caller validates the text (api/schemas/sender.py) and checks that the sender exists and that the label limit is not hit (api/routes/senders.py);
    a sender that is not in the archive fails the foreign key here and raises IntegrityError.
    Raises ProgrammingError (undefined table) on an archive that predates the migration - the route turns that into a 409.
    """
    try:
        row = conn.execute(
            sql_text(
                "INSERT INTO sender_display_names (sender_id, display_name) VALUES (:sender_id, :display_name) "
                "ON CONFLICT (sender_id) DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = now() "
                "RETURNING sender_id, display_name, updated_at"
            ),
            {"sender_id": sender_id, "display_name": display_name},
        ).mappings().one()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return dict(row)


def clear_sender_display_name(conn: Connection, sender_id: int, commit: bool = True) -> bool:
    """Remove a sender's label.
    Returns True if there was one, False if not (so the call is idempotent)."""
    try:
        row = conn.execute(
            sql_text("DELETE FROM sender_display_names WHERE sender_id = :sender_id RETURNING sender_id"),
            {"sender_id": sender_id},
        ).first()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return row is not None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def insert_message(
    conn: Connection,
    tg_message_id: int,
    chat_id: int,
    sender_id: int | None,
    text: str | None,
    date: datetime,
    is_edited: bool = False,
    commit: bool = True,
) -> int | None:
    """
    Store a new incoming or outgoing message.

    date (Telegram's send timestamp) is converted to local time before storage.
    archived_at is set to the current local time explicitly so it doesn't fall back to the DEFAULT now().

    `is_edited` should be True when this insert is a fallback from the edit handler - the message wasn't in the DB yet, but we know it has been edited at least once.

    commit=False lets a bulk caller (backfill.py) batch many inserts into one commit instead of forcing a WAL fsync after every single row
    - with hundreds of thousands of messages in one run, per-row commits meant per-row synchronous COMMITs, each one a round-trip fsync to Postgres's WAL before returning - batching amortizes that cost across many rows instead of paying it on every single one.
    The live handler path (on_message.py) is low-volume by nature
    (one real Telegram event at a time) and keeps the default commit=True - this only matters for bulk insert loops.

    Returns the internal row ID (messages.id) on success, or None if the message was already present (ON CONFLICT DO NOTHING - safe on re-delivery)
    """
    chat_id = resolve_chat_id(conn, chat_id)

    local_date = _localise(date)
    local_archived_at = _now()

    try:
        # Defensive FK upserts - ensure parent rows exist within this same transaction before the messages INSERT runs its FK check.
        #
        # In the normal flow, the caller already ran upsert_chat/upsert_sender (with commit=False), so these are always-safe no-ops on existing PKs.
        # They matter for edge cases where the parent rows weren't created first:
        #
        #   - Scheduled / auto-posted messages that bypass NewMessage (Telegram delivers them as updateShortSentMessage, which Telethon's NewMessage
        #     handler doesn't receive, so the chat is never seen before the edit).
        #   - Messages sent while TeleVault was offline - we only see the edit.
        #
        # ON CONFLICT DO NOTHING on an existing PK is a cheap, safe no-op.
        chat_stub = conn.execute(
            sql_text(
                "INSERT INTO chats (chat_id, name, chat_type) VALUES (:chat_id, NULL, 'group') "
                "ON CONFLICT (chat_id) DO NOTHING"
            ),
            {"chat_id": chat_id},
        )
        if chat_stub.rowcount > 0:
            logger.warning(
                f"insert_message: chat {chat_id} had no row before insert - "
                f"created a stub. Parent upsert may have been skipped."
            )

        if sender_id is not None:
            sender_stub = conn.execute(
                sql_text(
                    "INSERT INTO senders (sender_id, username, first_name, last_name) "
                    "VALUES (:sender_id, NULL, NULL, NULL) ON CONFLICT (sender_id) DO NOTHING"
                ),
                {"sender_id": sender_id},
            )
            if sender_stub.rowcount > 0:
                logger.warning(
                    f"insert_message: sender {sender_id} had no row before insert - "
                    f"created a stub. Parent upsert may have been skipped."
                )

        # RETURNING id replaces sqlite3's cursor.lastrowid, which has no Postgres/psycopg equivalent.
        # When ON CONFLICT DO NOTHING actually skips the insert, RETURNING yields zero rows,
        # so .first() below is None - giving exactly the same "already existed" signal the old rowcount check gave, just via a different mechanism.
        cursor = conn.execute(
            sql_text(
                "INSERT INTO messages (tg_message_id, chat_id, sender_id, text, date, is_edited, archived_at) "
                "VALUES (:tg_message_id, :chat_id, :sender_id, :text, :date, :is_edited, :archived_at) "
                "ON CONFLICT (tg_message_id, chat_id) DO NOTHING "
                "RETURNING id"
            ),
            {
                "tg_message_id": tg_message_id,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "text": text,
                "date": local_date,
                "is_edited": is_edited,
                "archived_at": local_archived_at,
            },
        )
        row = cursor.first()
        if commit:
            _commit(conn)
    except Exception:
        conn.rollback()
        raise

    row_id = row[0] if row is not None else None

    if row_id:
        logger.debug(
            f"Inserted message {tg_message_id} from chat {chat_id} -> internal id {row_id}"
        )
    else:
        logger.debug(
            f"Message {tg_message_id} in chat {chat_id} already exists - skipped."
        )

    return row_id


def _get_chat_type(conn: Connection, chat_id: int) -> str | None:
    """
    Look up a chat's stored type ('private', 'group', 'supergroup', 'channel').

    Internal helper for flag_deleted()'s channel-admin inference below - not exported for API use
    (db/read_queries.py has its own get_chat() for that, with a different return shape).
    Returns None if the chat isn't in the DB yet, which shouldn't normally happen for a chat_id that already has a message in it, but isn't assumed.
    """
    row = (
        conn.execute(
            sql_text("SELECT chat_type FROM chats WHERE chat_id = :chat_id"),
            {"chat_id": chat_id},
        )
        .mappings()
        .first()
    )
    return row["chat_type"] if row else None


def flag_deleted(
    conn: Connection,
    tg_message_id: int,
    chat_id: int,
    deleted_at: datetime | None = None,
    self_id: int | None = None,
) -> bool:
    """
    Mark a message as deleted and record a deletion snapshot.

    The snapshot (text at time of deletion) is written to message_deletions atomically with the flag update - both succeed or both roll back.

    Actor inference (deleted_by_inference):
    computed for two cases where it's a structural fact rather than a guess, everything else stays 'unknown' (the column's own DEFAULT):

      - Broadcast channels: only admins can delete channel posts, so any deletion there is 'channel_admin'.
      - Saved Messages (chat_id == self_id, the archiving account's own Telegram user ID):
        only the account owner has access to their own Saved Messages - no one else can even see it, let alone delete from it - so any deletion there is 'self'.

    Deliberately NOT attempted for ordinary private chats, groups, or supergroups:
    Telegram allows any party to delete a message for everyone with no time limit and no record of who did it,
    so a sender_id-based guess there would be closer to a coin flip than a signal.
    See api/schemas/message.py's DeletionOut docstring for the full reasoning.

    self_id is optional (defaults to None) so existing callers/tests that don't have it handy still work - Saved Messages just won't be detected without it,
    falling back to 'unknown' same as any other private chat.

    Note the distinction from "did this deletion event carry a chat_id" - Telegram's updateDeleteChannelMessages fires for supergroups too,
    not just channels (see handlers/on_delete.py's docstring), and supergroups behave like ordinary groups for deletion permissions.
    So chat_type is checked explicitly here rather than inferred from which code path called this function.

    Returns True if the row was found and flagged, False if the message wasn't in the DB (may have been sent before TeleVault was running).
    """
    ts = _localise(deleted_at) if deleted_at else _now()
    row = get_message(conn, tg_message_id, chat_id)

    if row is None or row["is_deleted"]:
        logger.warning(
            f"Deletion event for message {tg_message_id} in chat {chat_id} - not found in DB or already flagged (possibly sent before TeleVault was running)."
        )
        return False

    deleted_by_inference = "unknown"
    inference_confidence = None
    if _get_chat_type(conn, chat_id) == "channel":
        deleted_by_inference = "channel_admin"
        inference_confidence = (
            "Only a channel admin can delete a channel post - regular "
            "subscribers cannot delete posts, including their own."
        )
    elif self_id is not None and chat_id == self_id:
        deleted_by_inference = "self"
        inference_confidence = "Saved Messages is only accessible to you - no one else can see it, let alone delete from it."

    try:
        conn.execute(
            sql_text(
                "UPDATE messages SET is_deleted = :is_deleted, deleted_at = :deleted_at WHERE id = :id"
            ),
            {"is_deleted": True, "deleted_at": ts, "id": row["id"]},
        )
        conn.execute(
            sql_text("""
                INSERT INTO message_deletions
                    (message_id, text_snapshot, deleted_at, deleted_by_inference, inference_confidence)
                VALUES (:message_id, :text_snapshot, :deleted_at, :deleted_by_inference, :inference_confidence)
                """),
            {
                "message_id": row["id"],
                "text_snapshot": row["text"],
                "deleted_at": ts,
                "deleted_by_inference": deleted_by_inference,
                "inference_confidence": inference_confidence,
            },
        )
        _commit(conn)
    except Exception:
        conn.rollback()
        raise

    logger.info(
        f"Flagged message {tg_message_id} in chat {chat_id} as deleted at {ts}."
    )

    return True


def record_edit(
    conn: Connection,
    tg_message_id: int,
    chat_id: int,
    new_text: str | None,
    edited_at: datetime | None = None,
) -> bool:
    """
    Handle an edited message:
      1. Fetch the current text from messages (becomes old_text in the log).
      2. Insert a row into message_edits with old and new text.
      3. Update messages with the new text and mark is_edited = true.

    Steps 2 and 3 are committed atomically.

    Returns True on success, False if the message wasn't found in the DB.
    """
    ts = _localise(edited_at) if edited_at else _now()
    row = get_message(conn, tg_message_id, chat_id)

    if row is None:
        logger.warning(
            f"Edit event for message {tg_message_id} in chat {chat_id} "
            f"- not found in DB."
        )
        return False

    old_text = row["text"]
    internal_id = row["id"]

    # Telegram fires MessageEdited for non-text changes too: link preview
    # generation, inline keyboard updates, view count changes, etc.
    # If the text is identical, there's nothing useful to record.
    if old_text == new_text:
        logger.debug(
            f"Edit event for message {tg_message_id} in chat {chat_id} - "
            f"text unchanged (likely link preview or markup update). Skipping."
        )
        return True

    try:
        conn.execute(
            sql_text(
                "INSERT INTO message_edits (message_id, old_text, new_text, edited_at) "
                "VALUES (:message_id, :old_text, :new_text, :edited_at)"
            ),
            {
                "message_id": internal_id,
                "old_text": old_text,
                "new_text": new_text,
                "edited_at": ts,
            },
        )
        conn.execute(
            sql_text(
                "UPDATE messages SET text = :text, is_edited = :is_edited, edited_at = :edited_at WHERE id = :id"
            ),
            {"text": new_text, "is_edited": True, "edited_at": ts, "id": internal_id},
        )
        _commit(conn)
    except Exception:
        conn.rollback()
        raise

    logger.info(
        f"Recorded edit for message {tg_message_id} in chat {chat_id} "
        f"(internal id {internal_id})."
    )
    return True


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_message(
    conn: Connection, tg_message_id: int, chat_id: int
) -> dict[str, Any] | None:
    """
    Fetch a single message row by its Telegram ID + chat ID.
    Returns a dict-like mapping (supports row["col"] access) or None if not found.
    """
    cursor = conn.execute(
        sql_text(
            "SELECT * FROM messages WHERE tg_message_id = :tg_message_id AND chat_id = :chat_id"
        ),
        {"tg_message_id": tg_message_id, "chat_id": chat_id},
    )
    # .mappings().first() yields a SQLAlchemy RowMapping, which type checkers (rightly) don't treat as a plain dict;
    # converting makes the declared `dict[str, Any] | None` return type true, and every caller only does row["col"] access anyway.
    row = cursor.mappings().first()
    return dict(row) if row is not None else None


def get_deleted_messages(
    conn: Connection, chat_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """
    Retrieve deleted messages, optionally filtered by chat.
    Ordered newest-deleted first.

    Each row includes chat_name and chat_username from the joined chats table, so callers don't need a second query to display context.
    """
    if chat_id is not None:
        cursor = conn.execute(
            sql_text(
                "SELECT m.*, c.name AS chat_name, c.username AS chat_username"
                " FROM messages m"
                " JOIN chats c ON m.chat_id = c.chat_id"
                " WHERE m.is_deleted = true AND m.chat_id = :chat_id"
                " ORDER BY m.deleted_at DESC LIMIT :limit"
            ),
            {"chat_id": chat_id, "limit": limit},
        )
    else:
        cursor = conn.execute(
            sql_text(
                "SELECT m.*, c.name AS chat_name, c.username AS chat_username"
                " FROM messages m"
                " JOIN chats c ON m.chat_id = c.chat_id"
                " WHERE m.is_deleted = true"
                " ORDER BY m.deleted_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )

    return [dict(r) for r in cursor.mappings().all()]


def get_edit_history(
    conn: Connection, tg_message_id: int, chat_id: int
) -> list[dict[str, Any]]:
    """
    Return the full edit history for a message, oldest edit first.
    Returns an empty list if the message isn't in the DB.
    """
    row = get_message(conn, tg_message_id, chat_id)
    if row is None:
        return []

    cursor = conn.execute(
        sql_text(
            "SELECT * FROM message_edits WHERE message_id = :message_id ORDER BY edited_at ASC"
        ),
        {"message_id": row["id"]},
    )
    return [dict(r) for r in cursor.mappings().all()]


def get_deletion_record(
    conn: Connection, tg_message_id: int, chat_id: int
) -> dict[str, Any] | None:
    """
    Fetch the deletion record for a message, if one exists.

    Returns the message_deletions row (with text_snapshot and deleted_at) or None if the message was never flagged as deleted.
    """
    row = get_message(conn, tg_message_id, chat_id)
    if row is None:
        return None

    cursor = conn.execute(
        sql_text("SELECT * FROM message_deletions WHERE message_id = :message_id"),
        {"message_id": row["id"]},
    )
    deletion = cursor.mappings().first()
    return dict(deletion) if deletion is not None else None

# ---------------------------------------------------------------------------
# Removing a chat
# ---------------------------------------------------------------------------


def clear_all_data(conn: Connection) -> dict[str, Any]:
    """
    Remove everything the archiver has STORED from the archive, in ONE transaction: all messages (with their edit history and deletion snapshots),
    all chats, the group-migration records and the backfill history, and every sender except those the owner has given a display name.
    Returns the counts of what was removed.
    There is NO undo, and nothing here asks for confirmation - that belongs to the caller (the UI's typed confirmation).

    This is the "start from an empty archive" action (DELETE /api/archive/data); purge_chat() is its one-chat sibling.
    It is NOT account deletion: the full erasure the GDPR asks for (the account, its archive database, its Telegram session) is a separate, later feature.

    What is deliberately KEPT
        - The ingest blocklist and the archive-owner row: they are settings, not archived content.
          A block rule keeps its own copy of the chat's name.
        - The owner's display names (`sender_display_names`), and therefore the `senders` row each of them hangs on: the label has a foreign key to that row,
          so deleting the sender would silently delete the label too (ON DELETE CASCADE).
          Such a sender keeps only the identity it was first seen under (the same row the archiver would have created again);
          its recorded NAME HISTORY is removed with everything else.
          When that person writes again, the label simply applies again - which is the point of keeping it.
    What is removed that purge_chat() would keep
        - Every backfill run, including a global one ("all chats"): after a full clear there is nothing left for that log to describe.

    Concurrency (the live archiver keeps running while this executes)
        chats, senders and messages are locked in SHARE ROW EXCLUSIVE mode first,
        in the order the archiver itself touches them (upsert_chat, upsert_sender, insert_message), so the two cannot deadlock.
        A message that arrives meanwhile WAITS and is stored, into the now-empty archive, once this commits -
        none is lost (purge_chat() accepts a few-millisecond window; here the cost of a lost message is not worth paying for a rare, deliberate action).
        Without the lock, a message inserted between the statements below would make `DELETE FROM chats` fail on its foreign key and roll everything back.
        A running BACKFILL is the caller's concern, as for purge_chat(): the API refuses while one runs.

    Cost: like purge_chat(), deleting messages fires the per-row counter trigger (chats_counters_ad), so a very large archive takes a noticeable while;
    it stays one consistent transaction.
    TRUNCATE would be faster but is not used: it needs a privilege the archive login does not have and, with CASCADE, would also reach the display names.

    Any failure rolls the whole thing back, so a clear is all-or-nothing and the connection stays usable.
    """
    try:
        conn.execute(sql_text("LOCK TABLE chats, senders, messages IN SHARE ROW EXCLUSIVE MODE"))

        # Tables that are missing on archives that predate them (querying one would abort this whole transaction, see purge_chat()).
        has_labels = bool(conn.execute(sql_text("SELECT to_regclass('sender_display_names') IS NOT NULL")).scalar())
        has_history = bool(conn.execute(sql_text("SELECT to_regclass('sender_name_history') IS NOT NULL")).scalar())

        # Children first: message_edits and message_deletions point at messages.id with no ON DELETE CASCADE.
        deletions_deleted = conn.execute(sql_text("DELETE FROM message_deletions")).rowcount
        edits_deleted = conn.execute(sql_text("DELETE FROM message_edits")).rowcount
        messages_deleted = conn.execute(sql_text("DELETE FROM messages")).rowcount
        conn.execute(sql_text("DELETE FROM chat_migrations"))
        backfill_runs_deleted = conn.execute(sql_text("DELETE FROM backfill_runs")).rowcount
        chats_deleted = conn.execute(sql_text("DELETE FROM chats")).rowcount

        if has_history:
            conn.execute(sql_text("DELETE FROM sender_name_history"))
        if has_labels:
            senders_deleted = conn.execute(
                sql_text("DELETE FROM senders s WHERE NOT EXISTS (SELECT 1 FROM sender_display_names d WHERE d.sender_id = s.sender_id)")
            ).rowcount
            senders_kept = int(conn.execute(sql_text("SELECT COUNT(*) FROM senders")).scalar() or 0)
        else:
            senders_deleted = conn.execute(sql_text("DELETE FROM senders")).rowcount
            senders_kept = 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Counts only - never a name or any text (see the logging rules in the project docs).
    logger.info(
        "Cleared the archive: %d messages, %d edits, %d deletion records, %d chats, %d senders (%d kept for their display names), %d backfill runs.",
        messages_deleted, edits_deleted, deletions_deleted, chats_deleted, senders_deleted, senders_kept, backfill_runs_deleted,
    )
    return {
        "messages_deleted": messages_deleted,
        "edits_deleted": edits_deleted,
        "deletions_deleted": deletions_deleted,
        "chats_deleted": chats_deleted,
        "senders_deleted": senders_deleted,
        "senders_kept": senders_kept,
        "backfill_runs_deleted": backfill_runs_deleted,
    }


def purge_chat(conn: Connection, chat_id: int, *, also_unblock: bool = False) -> dict[str, Any] | None:
    """
    Remove every message-derived trace of one chat from the archive, in ONE transaction.
    Returns what was removed, or None if the chat is not in the archive.

    This is the owner's "forget this conversation" action (DELETE /api/chats/{chat_id}).
    It exists because an archive otherwise only grows:
    the chat of a Telegram account that was deleted keeps its name as a bare number, clutters every chat picker and can never be tidied up.
    There is NO undo, and nothing here asks for confirmation - that belongs to the caller (the UI's dialog).

    What is removed
        the chat's messages, their edit history and deletion snapshots
        (the old/new/snapshot TEXT goes too - a purge that kept a deleted message's snapshot would defeat its purpose),
        the `chats` row, the group-migration records that mention the chat,
        the backfill history of runs aimed ONLY at this chat (selector = its id or its @username; a run over every chat is a global log entry and stays),
        and every sender who has no message left anywhere in the archive.
    What is deliberately kept
        - An ingest-blocklist rule for the chat (`block_rule_kept` in the result), UNLESS the caller passes `also_unblock=True`.
          The rule has no foreign key to `chats` precisely so this works (db/schema.py):
          "delete what is stored AND keep ignoring the chat" must not silently stop ignoring it,
          so keeping is the default and lifting the block is always the owner's explicit, per-purge choice (a tick box in the confirmation dialog).
          The rule keeps its own copy of the chat's NAME, so that name is the one thing about the chat that survives a purge that keeps the rule.
          The rule is removed in the same transaction as everything else, so a failed purge never leaves "unblocked but not purged" behind.
        - Senders who also wrote in another chat, and any sender row that was already unreferenced before this call
          (only senders seen in THIS chat are candidates, so a purge never tidies up behind other chats).

    Concurrency (the live archiver keeps running while this executes)
        The `chats` row is locked first (SELECT ... FOR UPDATE).
        The archiver's insert_message() needs a share lock on that same row for its foreign key,
        so while a purge runs a new message for this chat WAITS instead of landing half-way.
        Once the purge commits:
        - if the message's own chat upsert came after the commit, the chat is created again, empty, from that message on (the behaviour the owner asked for);
        - the one message that was already past its upsert but still waiting on the lock fails its foreign-key check and is lost.
          That window is a few milliseconds wide per purge and the archiver logs the failure; it is not worth a retry loop in a handler that must never block.
        A running BACKFILL is the caller's concern, not this function's: it could re-insert the very history being removed, so the API refuses a purge while one runs.

    Cost: deleting messages fires the per-row counter trigger (chats_counters_ad), which updates the chat row once per message.
    That is linear and fine for a normal chat; a chat of hundreds of thousands of messages takes a noticeable while but stays one consistent transaction.

    Any failure rolls the whole thing back, so a purge is all-or-nothing and the connection stays usable.
    """
    params = {"chat_id": chat_id}
    try:
        chat = conn.execute(sql_text("SELECT username FROM chats WHERE chat_id = :chat_id FOR UPDATE"), params).first()
        if chat is None:
            conn.rollback()
            return None
        username = chat[0]

        # The blocklist table is missing on archives that predate it; look the rule up only if the table exists
        # (querying a missing table would abort this whole transaction - see is_chat_blocked() for the same hazard).
        block_rule_kept = False
        if conn.execute(sql_text("SELECT to_regclass('ingest_blocked_chats') IS NOT NULL")).scalar():
            if also_unblock:
                conn.execute(sql_text("DELETE FROM ingest_blocked_chats WHERE chat_id = :chat_id"), params)
            else:
                block_rule_kept = conn.execute(sql_text("SELECT 1 FROM ingest_blocked_chats WHERE chat_id = :chat_id"), params).first() is not None

        messages_deleted = int(conn.execute(sql_text("SELECT COUNT(*) FROM messages WHERE chat_id = :chat_id"), params).scalar() or 0)

        # Children first: message_edits and message_deletions point at messages.id with no ON DELETE CASCADE.
        deletions_deleted = conn.execute(
            sql_text("DELETE FROM message_deletions d USING messages m WHERE d.message_id = m.id AND m.chat_id = :chat_id"), params
        ).rowcount
        edits_deleted = conn.execute(
            sql_text("DELETE FROM message_edits e USING messages m WHERE e.message_id = m.id AND m.chat_id = :chat_id"), params
        ).rowcount

        # Messages and the senders that become unreferenced, in ONE statement: the messages.sender_id foreign key is checked at the end of the statement,
        # when the messages are already gone, so a sender can be deleted together with the last message that mentioned it.
        # `m.chat_id <> :chat_id` is needed because the NOT EXISTS subquery still sees the messages this very statement deletes.
        senders_deleted = conn.execute(
            sql_text(
                """
                WITH gone AS (
                    DELETE FROM messages WHERE chat_id = :chat_id RETURNING sender_id
                ),
                candidates AS (
                    SELECT DISTINCT sender_id FROM gone WHERE sender_id IS NOT NULL
                )
                DELETE FROM senders s USING candidates c
                WHERE s.sender_id = c.sender_id
                  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.sender_id = s.sender_id AND m.chat_id <> :chat_id)
                """
            ),
            params,
        ).rowcount

        conn.execute(sql_text("DELETE FROM chat_migrations WHERE old_chat_id = :chat_id OR new_chat_id = :chat_id"), params)

        # A backfill started for one chat stores that chat as its selector: a numeric id, or an @username (any case, "@" optional).
        # LTRIM(.., '@') + LOWER mirror how Telegram treats handles. CAST: a NULL username must not leave the parameter's type undecidable.
        backfill_runs_deleted = conn.execute(
            sql_text(
                "DELETE FROM backfill_runs WHERE chat_selector = :chat_id_text "
                "OR LOWER(LTRIM(chat_selector, '@')) = LOWER(CAST(:username AS text))"
            ),
            {"chat_id_text": str(chat_id), "username": username},
        ).rowcount

        conn.execute(sql_text("DELETE FROM chats WHERE chat_id = :chat_id"), params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Counts only - never a name or any text (see the logging rules in the project docs).
    logger.info(
        "Purged chat %s: %d messages, %d edits, %d deletion records, %d senders, %d backfill runs.",
        chat_id, messages_deleted, edits_deleted, deletions_deleted, senders_deleted, backfill_runs_deleted,
    )
    return {
        "chat_id": chat_id,
        "messages_deleted": messages_deleted,
        "edits_deleted": edits_deleted,
        "deletions_deleted": deletions_deleted,
        "senders_deleted": senders_deleted,
        "backfill_runs_deleted": backfill_runs_deleted,
        "block_rule_kept": block_rule_kept,
    }