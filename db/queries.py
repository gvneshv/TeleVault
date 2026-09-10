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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


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
    Insert a chat record if it doesn't exist yet.

    The ``username`` is the @handle - present for public groups and channels, None for private chats and legacy groups without a public link.
    Stored as-is without the leading '@' for cleaner querying.

    Existing rows are left untouched (ON CONFLICT DO NOTHING). Name/username changes over time are not tracked yet - that's a future feature.

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
                "ON CONFLICT (chat_id) DO NOTHING"
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

    # Fold any pre-existing rows under old_chat_id in immediately, rather than waiting for a manual merge_migrated_chats.py run.
    # Safe every time - merge_chat() is a no-op if old_chat_id already has nothing left.
    merge_chat(conn, old_chat_id, new_chat_id)


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
    Same rationale as upsert_chat - we preserve the first-seen identity.

    Note: for anonymous admin posts in supergroups, Telegram sets the sender to the group itself, so sender_id may be a negative channel ID rather than a user ID. These rows end up in the senders table with whatever fields the channel entity exposes (usually just a name/username).
    This is a Telegram protocol behaviour, not a bug.

    commit=False: same semantics as upsert_chat - see its docstring.
    When commit=True, a RuntimeError is raised if the row is absent after insert with no exception - unexpected,
    since senders has no CHECK constraint (unlike chats) that could cause a legitimate silent rejection.
    """
    try:
        conn.execute(
            sql_text(
                "INSERT INTO senders (sender_id, username, first_name, last_name) "
                "VALUES (:sender_id, :username, :first_name, :last_name) "
                "ON CONFLICT (sender_id) DO NOTHING"
            ),
            {
                "sender_id": sender_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
            },
        )
    except Exception:
        # No CHECK constraint on senders to translate into a friendlier message the way upsert_chat does - but any error here still aborts the Postgres transaction,
        # so it must still be rolled back before re-raising (see this module's docstring).
        conn.rollback()
        raise

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

    Internal helper for flag_deleted()'s channel-admin inference below — not exported for API use
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
        only the account owner has access to their own Saved Messages — no one else can even see it, let alone delete from it — so any deletion there is 'self'.

    Deliberately NOT attempted for ordinary private chats, groups, or supergroups:
    Telegram allows any party to delete a message for everyone with no time limit and no record of who did it,
    so a sender_id-based guess there would be closer to a coin flip than a signal.
    See api/schemas/message.py's DeletionOut docstring for the full reasoning.

    self_id is optional (defaults to None) so existing callers/tests that don't have it handy still work — Saved Messages just won't be detected without it,
    falling back to 'unknown' same as any other private chat.

    Note the distinction from "did this deletion event carry a chat_id" — Telegram's updateDeleteChannelMessages fires for supergroups too,
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
            f"Edit event for message {tg_message_id} in chat {chat_id} — "
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
    return cursor.mappings().first()


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
    return cursor.mappings().first()