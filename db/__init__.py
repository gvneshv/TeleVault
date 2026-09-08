"""
db - TeleVault database package.
 
Re-exports the public interface so the rest of the application imports from one place rather than knowing which submodule each symbol lives in:
 
    from db import init_db, queries

Internal reorganisation (splitting queries.py, moving connection logic, etc.) won't break any caller as long as these exports stay stable.

apply_schema is intentionally NOT exported (removed as part of the Postgres migration - see db/schema.py's module docstring).
Schema application is now `alembic upgrade head`, run explicitly, not a function called from application code at every startup.
Any remaining `db.apply_schema(conn)` call site (main.py, backfill.py, as of this change)
will raise AttributeErroruntil those call sites are updated to drop the call entirely.
"""

from db.connection import init_db, close_db, get_connection, get_readonly_connection
from db import queries
from .read_queries import (
    get_chats,
    get_chat,
    get_messages,
    get_chat_messages,
    get_message_detail,
    get_stats,
    get_message_count,
)


__all__ = ["init_db", "close_db", "get_connection", "get_readonly_connection", "queries", "get_chats", "get_chat", "get_messages", "get_chat_messages", "get_message_detail", "get_stats", "get_message_count"]