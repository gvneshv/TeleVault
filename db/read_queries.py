"""
Read-only query layer used exclusively by the API server.

All functions accept an SQLAlchemy Connection opened read-only
(see db/connection.py's get_readonly_connection()) and return plain dicts or lists of dicts - no ORM, no Pydantic here.
Pydantic validation happens in the route layer so this module stays dependency-free and easily testable.

Pagination convention throughout:
    page     : 1-based page number
    per_page : rows per page (capped at 200 in the API layer)
    Returns  : {"items": [...], "total": int}
    The caller assembles the full PaginatedResponse envelope.

All datetimes are TIMESTAMPTZ;
returned as Python datetime objects
(psycopg converts natively - unlike the SQLite version, there's no ISO-string step here, since there was never a string to begin with).
"""

import re
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _like_pattern(q: str) -> str:
    """
    Build a safe ILIKE pattern treating the whole input as a literal substring,
    not as LIKE wildcard syntax - % and _ are wildcards to LIKE/ILIKE,
    so an unescaped user search containing either would silently match more (or differently) than the literal text the person typed.

    Replaces db/read_queries.py's old _fts5_phrase_query(),
    which existed for the same reason under FTS5's MATCH syntax
    (different special characters, same underlying goal: treat user input as a literal phrase).

    Backslash is Postgres's default LIKE/ILIKE escape character, so escaping backslash, %, and _ here
    (in that order, to avoid double-escaping the backslashes just inserted) is sufficient - no explicit ESCAPE clause is needed in the query itself.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _rows_to_dicts(cursor) -> list[dict[str, Any]]:
    """Convert all cursor rows to plain dicts keyed by column name."""
    return [dict(r) for r in cursor.mappings().all()]


def _shape_message_row(row: dict[str, Any], has_chat_columns: bool) -> dict[str, Any]:
    """
    Reshape a flat SQL row into the nested structure MessageOut/MessageDetail actually expect: {"chat": {...} | None, "sender": {...} | None, ...}.

    BUG THIS FIXES: without this step, a flat row (e.g. with a top-level "sender_id" and "chat_name" key, as produced by dict(zip(cols, row)))
    has no key literally named "chat" or "sender" at all.
    FastAPI/Pydantic validates the dict against MessageOut, finds neither key,
    and silently falls back to each field's declared default of None — for every row, always, regardless of the SQL join actually having the data.
    This was invisible in testing because Optional[None] doesn't raise a validation error; it just quietly renders as "—" in the frontend.
    Confirmed via testing that this affected 100% of rows in the Messages and Deleted views, not an occasional edge case.

    Args:
        row              : flat dict with message columns, sender_* columns, and chat_name/chat_type if has_chat_columns.
        has_chat_columns : whether this row's SELECT joined the chats table.
                           False for get_chat_messages() (chat is intentionally omitted there — see its docstring), True for get_messages() and get_message_detail().
    """
    sender = None
    if row["sender_id"] is not None:
        sender = {
            "sender_id": row["sender_id"],
            "username": row["sender_username"],
            "first_name": row["sender_first_name"],
            "last_name": row["sender_last_name"],
            # Not stored yet — Phase 3 feature, see SenderOut's docstring.
            "display_name": None,
        }

    chat = None
    if has_chat_columns:
        chat = {
            "chat_id": row["chat_id"],
            "name": row.get("chat_name"),
            "chat_type": row.get("chat_type"),
        }

    return {
        "id": row["id"],
        "tg_message_id": row["tg_message_id"],
        "chat": chat,
        "sender": sender,
        "text": row["text"],
        "date": row["date"],
        "archived_at": row["archived_at"],
        "is_edited": row["is_edited"],
        "edited_at": row["edited_at"],
        "is_deleted": row["is_deleted"],
        "deleted_at": row["deleted_at"],
    }


def _paginate(
    query: str, params: dict, conn: Connection, page: int, per_page: int
) -> dict[str, Any]:
    """
    Run a SELECT query with LIMIT/OFFSET pagination.

    Wraps the caller's query in a COUNT subquery to get the total without a second round-trip, then fetches the page.

    Args:
        query    : SQL without LIMIT/OFFSET — must be a SELECT.
        params   : Named parameters matching the query's :placeholders.
        conn     : Open read-only connection.
        page     : 1-based page number.
        per_page : Rows per page.

    Returns:
        {"items": list[dict], "total": int}

    Note the "AS count_sub" alias below - unlike SQLite, Postgres requires every subquery in a FROM clause to be aliased.
    """
    count_sql = f"SELECT COUNT(*) FROM ({query}) AS count_sub"
    total: int = conn.execute(sql_text(count_sql), params).scalar()

    offset = (page - 1) * per_page
    paged_sql = f"{query} LIMIT :_limit OFFSET :_offset"
    cursor = conn.execute(
        sql_text(paged_sql), {**params, "_limit": per_page, "_offset": offset}
    )
    items = _rows_to_dicts(cursor)

    return {"items": items, "total": total}


def _is_whole_word_match(text: str, word: str) -> bool:
    """
    Check if `word` appears in `text` as a standalone word, not merely as a substring.
    Uses lookaround instead of \\b so Unicode word boundaries (Cyrillic included) are handled correctly.
    """
    pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _paginate_filtered(
    query: str, params: dict, conn: Connection, page: int, per_page: int, predicate
) -> dict[str, Any]:
    """
    Like _paginate(), but for filters that can't be expressed in SQL (whole-word matching over pg_trgm's substring results).
    Fetches every matching row - no LIMIT/OFFSET at the SQL level - applies `predicate` to each row, then paginates the filtered list in Python.
    Only used for whole-word search.
    """
    cursor = conn.execute(sql_text(query), params)
    all_rows = _rows_to_dicts(cursor)
    filtered = [r for r in all_rows if predicate(r)]
    offset = (page - 1) * per_page
    return {"items": filtered[offset : offset + per_page], "total": len(filtered)}


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


def get_chats(
    conn: Connection,
    page: int = 1,
    per_page: int = 50,
    order: str = "desc",
) -> dict[str, Any]:
    """
    Return all known chats sorted by most recent activity (i.e. the same ordering as the Telegram sidebar) by default.

    message_count/deleted_count/last_message_at/last_message_preview are denormalized columns on chats itself
    (see the chats-counter triggers in the Alembic baseline migration),
    kept in sync by triggers on the messages table - this used to be a LEFT JOIN + GROUP BY across every message on every request,
    which got slow as the archive grew since it always scanned the whole messages table regardless of which page was requested.
    Now it's a plain, constant-cost read over the (small) chats table.

    order: "desc" (most recently active first, default) or "asc" (least recently active / longest untouched first).
    Chats with no messages yet (last_message_at IS NULL) always sort last regardless of direction - there's no "recency" to invert for them,
    and surfacing empty chats first on an asc sort would be more confusing than helpful. (NULLS LAST works identically in Postgres and SQLite here.)

    Columns returned:
        chat_id, name, username, chat_type, first_seen, message_count, deleted_count, last_message_at, last_message_preview
    """
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"""
        SELECT
            chat_id, name, username, chat_type, first_seen,
            message_count, deleted_count, last_message_at, last_message_preview
        FROM chats
        ORDER BY last_message_at {direction} NULLS LAST
    """
    return _paginate(sql, {}, conn, page, per_page)


def get_chat(conn: Connection, chat_id: int) -> dict[str, Any] | None:
    """
    Return a single chat record with aggregate counts.
    Returns None if the chat_id is not in the database.

    See get_chats()'s docstring - counts are denormalized columns, not computed here.
    """
    sql = """
        SELECT
            chat_id, name, username, chat_type, first_seen,
            message_count, deleted_count, last_message_at, last_message_preview
        FROM chats
        WHERE chat_id = :chat_id
    """
    row = conn.execute(sql_text(sql), {"chat_id": chat_id}).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _base_message_select() -> str:
    """
    Core SELECT joining messages -> senders.
    Used by both get_messages() and get_chat_messages() to keep column lists consistent — changing the projection in one place changes both.

    Note: chat columns are NOT joined here;
    get_messages() adds them, while get_chat_messages() omits them (they're redundant per-chat).
    """
    return """
        SELECT
            m.id,
            m.tg_message_id,
            m.chat_id,
            m.text,
            m.date,
            m.archived_at,
            m.is_edited,
            m.edited_at,
            m.is_deleted,
            m.deleted_at,
            -- sender fields prefixed to avoid collision with message columns
            s.sender_id    AS sender_id,
            s.username     AS sender_username,
            s.first_name   AS sender_first_name,
            s.last_name    AS sender_last_name
        FROM messages m
        LEFT JOIN senders s ON s.sender_id = m.sender_id
    """


def get_messages(
    conn: Connection,
    page: int = 1,
    per_page: int = 50,
    q: str | None = None,
    chat_id: int | None = None,
    sender_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_deleted: bool = False,
    only_edited: bool = False,
    whole_word: bool = False,
    order: str = "desc",
) -> dict[str, Any]:
    """
    Global message search with optional filters.

    Used by:
        GET /api/messages          (global feed)
        GET /api/deleted           (only_deleted=True)

    Filter behaviour:
        q            : case-insensitive substring match on message text.
        chat_id      : restrict to one chat.
        sender_id    : restrict to one sender.
        date_from    : ISO 8601 string, inclusive lower bound on m.date.
        date_to      : ISO 8601 string, inclusive upper bound on m.date.
        only_deleted : if True, return only messages where is_deleted is true.
        only_edited  : if True, return only messages where is_edited is true.
        order        : "desc" (newest first, default) or "asc" (oldest first).
                       Validated by the route layer (Literal["asc","desc"]) before reaching here,
                       but re-checked with a plain if/else rather than trusted and interpolated directly - this string ends up in the SQL text
                       (can't be a bound parameter for ORDER BY direction), so a stray value must fall back safely, not open a SQL-injection path.

    Also joins chat name/type for inline display (avoids a second request per row on the global feed and deleted views).

    Search implementation:
    an ILIKE substring match, backed by the pg_trgm GIN index on messages.text (idx_messages_text_trgm)
    - replaces SQLite's FTS5 MATCH against a separate messages_fts virtual table.
    Same substring-anywhere, case-insensitive, Cyrillic-friendly behaviour as before,
    just via a different mechanism (see db/schema.py's module docstring for why pg_trgm was chosen).
    """
    select = """
        SELECT
            m.id,
            m.tg_message_id,
            m.chat_id,
            c.name         AS chat_name,
            c.chat_type    AS chat_type,
            m.text,
            m.date,
            m.archived_at,
            m.is_edited,
            m.edited_at,
            m.is_deleted,
            m.deleted_at,
            s.sender_id    AS sender_id,
            s.username     AS sender_username,
            s.first_name   AS sender_first_name,
            s.last_name    AS sender_last_name
        FROM messages m
        LEFT JOIN senders s ON s.sender_id = m.sender_id
        LEFT JOIN chats c   ON c.chat_id   = m.chat_id
    """

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if q:
        conditions.append("m.text ILIKE :q_pattern")
        params["q_pattern"] = _like_pattern(q)
    if chat_id is not None:
        conditions.append("m.chat_id = :chat_id")
        params["chat_id"] = chat_id
    if sender_id is not None:
        conditions.append("m.sender_id = :sender_id")
        params["sender_id"] = sender_id
    if date_from:
        conditions.append("m.date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("m.date <= :date_to")
        params["date_to"] = date_to
    if only_deleted:
        conditions.append("m.is_deleted = true")
    if only_edited:
        conditions.append("m.is_edited = true")

    direction = "ASC" if order == "asc" else "DESC"
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"{select} {where} ORDER BY m.date {direction}"

    if whole_word and q:
        result = _paginate_filtered(
            sql,
            params,
            conn,
            page,
            per_page,
            predicate=lambda r: _is_whole_word_match(r["text"] or "", q),
        )
    else:
        result = _paginate(sql, params, conn, page, per_page)

    result["items"] = [
        _shape_message_row(r, has_chat_columns=True) for r in result["items"]
    ]
    return result


def get_chat_messages(
    conn: Connection,
    chat_id: int,
    page: int = 1,
    per_page: int = 50,
    q: str | None = None,
    sender_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    whole_word: bool = False,
    order: str = "desc",
) -> dict[str, Any]:
    """
    Messages within a single chat, newest first by default (see `order`).

    Used by GET /api/chats/{chat_id}/messages.
    Chat columns are omitted (redundant in a per-chat context).
    All other filters mirror get_messages().
    """
    select = _base_message_select()

    conditions: list[str] = ["m.chat_id = :chat_id"]
    params: dict[str, Any] = {"chat_id": chat_id}

    if q:
        conditions.append("m.text ILIKE :q_pattern")
        params["q_pattern"] = _like_pattern(q)
    if sender_id is not None:
        conditions.append("m.sender_id = :sender_id")
        params["sender_id"] = sender_id
    if date_from:
        conditions.append("m.date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("m.date <= :date_to")
        params["date_to"] = date_to

    direction = "ASC" if order == "asc" else "DESC"
    where = "WHERE " + " AND ".join(conditions)
    sql = f"{select} {where} ORDER BY m.date {direction}"

    if whole_word and q:
        result = _paginate_filtered(
            sql,
            params,
            conn,
            page,
            per_page,
            predicate=lambda r: _is_whole_word_match(r["text"] or "", q),
        )
    else:
        result = _paginate(sql, params, conn, page, per_page)

    result["items"] = [
        _shape_message_row(r, has_chat_columns=False) for r in result["items"]
    ]
    return result


def get_message_detail(
    conn: Connection,
    message_id: int,
) -> dict[str, Any] | None:
    """
    Single message with full edit history and deletion record.
    Used by GET /api/messages/{id}.

    Returns None if message_id does not exist.

    Edits are attached as a list under the key 'edits'.
    Deletion record (if any) is attached under the key 'deletion'.
    """
    # Base message row.
    # Previously used _base_message_select() (sender-only, no chats join)
    # — added the chats join explicitly here since this endpoint's response (MessageDetail) includes a chat field same as MessageOut,
    # and there was no documented reason to omit it the way get_chat_messages() intentionally does.
    select = """
        SELECT
            m.id,
            m.tg_message_id,
            m.chat_id,
            c.name         AS chat_name,
            c.chat_type    AS chat_type,
            m.text,
            m.date,
            m.archived_at,
            m.is_edited,
            m.edited_at,
            m.is_deleted,
            m.deleted_at,
            s.sender_id    AS sender_id,
            s.username     AS sender_username,
            s.first_name   AS sender_first_name,
            s.last_name    AS sender_last_name
        FROM messages m
        LEFT JOIN senders s ON s.sender_id = m.sender_id
        LEFT JOIN chats c   ON c.chat_id   = m.chat_id
    """
    row = (
        conn.execute(
            sql_text(f"{select} WHERE m.id = :message_id"), {"message_id": message_id}
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    result = _shape_message_row(dict(row), has_chat_columns=True)

    # Edit history — oldest first so the UI can render a timeline
    edits_cursor = conn.execute(
        sql_text("""
            SELECT id, old_text, new_text, edited_at
            FROM message_edits
            WHERE message_id = :message_id
            ORDER BY edited_at ASC
            """),
        {"message_id": message_id},
    )
    result["edits"] = _rows_to_dicts(edits_cursor)

    # Deletion record with actor inference
    del_row = (
        conn.execute(
            sql_text("""
            SELECT id, text_snapshot, deleted_at, deleted_by_inference, inference_confidence
            FROM message_deletions
            WHERE message_id = :message_id
            LIMIT 1
            """),
            {"message_id": message_id},
        )
        .mappings()
        .first()
    )
    result["deletion"] = dict(del_row) if del_row else None

    return result


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def get_stats(conn: Connection) -> dict[str, Any]:
    """
    Aggregate statistics for the /api/stats endpoint.

    Runs three queries:
        1. Global totals - SUM over the same denormalized per-chat counters the per-chat breakdown below already uses
        (see the chats-counter triggers in the Alembic baseline migration),
        instead of the COUNT(*)+SUM(CASE...) scan across every row in messages this used to run on every request.
        archiving_since is served by a primary-key-order lookup instead: `id` is a BigInteger IDENTITY column,
        so (barring a rollback leaving a gap) the lowest id is the first-ever archived row, and Postgres answers
        "smallest id" straight from the primary key's own index without scanning any other row.
        2. total_chats / total_senders - trivial COUNT(*) on small tables.
        3. Per-chat breakdown - reads the denormalized counters on chats directly instead of a LEFT JOIN + GROUP BY across every message,
        which used to make this endpoint slower the larger the archive grew.

    Stats are not latency-critical - they're for a dashboard, not a hot path -
    but there's no reason for it to be slow just because it's not urgent.
    """
    totals_row = conn.execute(sql_text("""
            SELECT
                COALESCE(SUM(message_count), 0) AS total_messages,
                COALESCE(SUM(deleted_count), 0) AS total_deleted,
                COALESCE(SUM(edited_count), 0)  AS total_edited
            FROM chats
            """)).first()

    archiving_since = conn.execute(
        sql_text("SELECT archived_at FROM messages ORDER BY id ASC LIMIT 1")
    ).scalar()

    total_chats = conn.execute(sql_text("SELECT COUNT(*) FROM chats")).scalar()
    total_senders = conn.execute(sql_text("SELECT COUNT(*) FROM senders")).scalar()

    per_chat_cursor = conn.execute(sql_text("""
            SELECT
                chat_id, name, chat_type,
                message_count, deleted_count, edited_count,
                first_message_at, last_message_at
            FROM chats
            ORDER BY message_count DESC
            """))
    per_chat = _rows_to_dicts(per_chat_cursor)

    return {
        "total_messages": totals_row[0] or 0,
        "total_deleted": totals_row[1] or 0,
        "total_edited": totals_row[2] or 0,
        "total_chats": total_chats,
        "total_senders": total_senders,
        "archiving_since": archiving_since,
        "per_chat": per_chat,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def get_message_count(conn: Connection) -> int:
    """
    Quick sanity check used by the health endpoint.
    Returns total row count from the messages table.
    """
    return conn.execute(sql_text("SELECT COUNT(*) FROM messages")).scalar()