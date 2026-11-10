"""
The ingestion-time filters - what TeleVault must NOT archive - for the CALLING account only.

Chat blocklist (which chats are not archived):
    GET    /api/ingest/blocklist            - the account's blocked chats
    PUT    /api/ingest/blocklist/{chat_id}  - block a chat (idempotent)
    DELETE /api/ingest/blocklist/{chat_id}  - stop blocking a chat (idempotent)

Message ignore rules (which plain-text messages are not archived):
    GET    /api/ingest/ignore-rules         - the account's rules, with the limits the form needs
    POST   /api/ingest/ignore-rules         - add a rule: a phrase, for all chats or for the listed ones
    DELETE /api/ingest/ignore-rules/{id}    - remove a rule (idempotent)

How it works (the full policy is in handlers/ingest_filter.py, the table in db/schema.py's ingest_blocked_chats):
    The live archiver and backfill look a chat up in this list before they read or store anything from it, once per event / per chat.
    So a change made here takes effect on the next message, with no restart and nothing for this API to push to the workers.
    Everything not listed is archived - a new chat is always allowed until the owner blocks it here.

What blocking does NOT do:
    It stops FUTURE archiving only. Messages already archived stay, and show up in the views as before; removing them is a separate, destructive
    action that does not exist yet. Unblocking likewise recovers nothing - what arrived while the chat was blocked was never stored.

Only chats the archive already knows can be blocked (PUT answers 404 otherwise): the name shown in the list is copied from the archive's own record of the chat,
so the client never supplies free text, and there is nothing to validate beyond the id.

Writes go through api.dependencies.get_archive_write_connection (read-write); the GET uses the usual read-only get_archive_connection.
Both resolve the CALLER's own archive, so one account can never read or change another's blocklist.

Message ignore rules (policy: handlers/ingest_filter.py, matching: utils/text_matching.py, tables: db/schema.py):
    A message is ignored when its TEXT contains the rule's phrase, ignoring case - never a regular expression.
    Captions and file names are not matched, only the text of a message that carries no file.
    Like the blocklist, a rule only affects FUTURE messages: what is already archived stays, and nothing that was ignored can be recovered later.
    A rule applies to every chat, or only to the chats it lists; as with the blocklist, only chats the archive already knows can be listed
    (their names are copied from the archive, so the client supplies the phrase and nothing else as free text).
    The phrase itself is never logged anywhere.

Archives created before these features have no tables until `python scripts/migrate_archives.py` is run (RUNBOOK.md).
Every route here answers 409 "archive_outdated" for such an archive instead of a 500; the workers meanwhile simply archive everything, as they always did.
"""

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

import db
from api.dependencies import get_archive_connection, get_archive_write_connection
from api.schemas import (
    BlockedChatOut,
    BlockedChatRemovedOut,
    BlocklistOut,
    IgnoreRuleIn,
    IgnoreRuleOut,
    IgnoreRuleRemovedOut,
    IgnoreRulesOut,
)
from db.read_queries import get_blocked_chats, get_chat, get_ignore_rules
from utils.validation import (
    IGNORE_RULE_MAX_CHATS,
    IGNORE_RULE_MAX_COUNT,
    IGNORE_RULE_MAX_LENGTH,
    INGEST_BLOCKLIST_MAX_COUNT,
    TELEGRAM_CHAT_ID_MAX,
    TELEGRAM_CHAT_ID_MIN,
)

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Every route takes the chat id from the URL path; bounded so an absurd value is a 422 here instead of a numeric-overflow error from Postgres.
_ChatId = Path(ge=TELEGRAM_CHAT_ID_MIN, le=TELEGRAM_CHAT_ID_MAX, description="Telegram chat id, as shown in the archive.")


# A rule id is the BIGINT identity of ingest_ignore_rules: positive, and bounded so an absurd value is a 422 and not a numeric-overflow error from Postgres.
_RuleId = Path(ge=1, le=2**63 - 1, description="The rule's id, as listed.")


@contextmanager
def _needs_migrated_archive():
    """
    Turn "this archive has no ingest_blocked_chats / ingest_ignore_rules table" into a clear 409 the UI can translate (reason "archive_outdated").
    Any OTHER database error is not ours to explain and propagates untouched.
    """
    try:
        yield
    except ProgrammingError as exc:
        if not db.is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Your archive database needs updating before chats can be blocked or messages ignored. Run `python scripts/migrate_archives.py` - see RUNBOOK.md.",
                "reason": "archive_outdated",
            },
        ) from exc


@router.get(
    "/blocklist",
    response_model=BlocklistOut,
    summary="List the chats that are not being archived",
)
def list_blocklist(conn: Connection = Depends(get_archive_connection)) -> dict[str, Any]:
    """Every chat on the caller's blocklist, newest block first, with the most that may be blocked (`max_count`)."""
    with _needs_migrated_archive():
        items = get_blocked_chats(conn)
    return {"items": items, "total": len(items), "max_count": INGEST_BLOCKLIST_MAX_COUNT}


@router.put(
    "/blocklist/{chat_id}",
    response_model=BlockedChatOut,
    summary="Stop archiving a chat",
)
def block_chat(chat_id: int = _ChatId, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """
    Block a chat: from the next message on it is neither archived nor edit-tracked. Already-archived messages are untouched.

    Idempotent - blocking a chat that is already blocked returns its existing rule. Otherwise:
        404 "chat_not_found" - the archive has never seen this chat (only chats it already knows can be blocked)
        409 "blocklist_full" - the account is at INGEST_BLOCKLIST_MAX_COUNT rules
    `chat_id` may be an old id of a group that has since become a supergroup; the rule is stored under the current one.
    """
    with _needs_migrated_archive():
        rules = get_blocked_chats(conn)
        canonical = db.queries.resolve_chat_id(conn, chat_id)

        existing = next((r for r in rules if r["chat_id"] == canonical), None)
        if existing is not None:
            return existing

        chat = get_chat(conn, canonical)
        if chat is None:
            raise HTTPException(
                status_code=404,
                detail={"message": f"Chat {chat_id} is not in the archive, so it cannot be blocked.", "reason": "chat_not_found"},
            )
        if len(rules) >= INGEST_BLOCKLIST_MAX_COUNT:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"At most {INGEST_BLOCKLIST_MAX_COUNT} chats can be blocked. Unblock one first.",
                    "reason": "blocklist_full",
                },
            )

        db.queries.block_chat(conn, canonical, chat["name"])
        return next(r for r in get_blocked_chats(conn) if r["chat_id"] == canonical)


@router.delete(
    "/blocklist/{chat_id}",
    response_model=BlockedChatRemovedOut,
    summary="Archive a chat again",
)
def unblock_chat(chat_id: int = _ChatId, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """
    Remove a chat from the blocklist: archiving resumes from the next message. Messages that arrived while it was blocked were never stored and cannot be recovered.
    Idempotent - `removed` is False if the chat was not blocked.
    """
    with _needs_migrated_archive():
        removed = db.queries.unblock_chat(conn, chat_id)
    return {"chat_id": chat_id, "removed": removed}


# ---------------------------------------------------------------------------
# Message ignore rules
# ---------------------------------------------------------------------------


def _ignore_rule_out(conn: Connection, rule_id: int) -> dict[str, Any]:
    """The rule as the API returns it.
    Read back after a write so the response is exactly what the next GET would show."""
    return next(r for r in get_ignore_rules(conn) if r["id"] == rule_id)


@router.get(
    "/ignore-rules",
    response_model=IgnoreRulesOut,
    summary="List the message ignore rules",
)
def list_ignore_rules(conn: Connection = Depends(get_archive_connection)) -> dict[str, Any]:
    """Every rule of the caller's archive, newest first, with the limits the form needs (`max_count`, `max_length`, `max_chats`)."""
    with _needs_migrated_archive():
        items = get_ignore_rules(conn)
    return {
        "items": items,
        "total": len(items),
        "max_count": IGNORE_RULE_MAX_COUNT,
        "max_length": IGNORE_RULE_MAX_LENGTH,
        "max_chats": IGNORE_RULE_MAX_CHATS,
    }


@router.post(
    "/ignore-rules",
    response_model=IgnoreRuleOut,
    status_code=201,
    summary="Stop archiving messages that contain a phrase",
)
def add_ignore_rule(body: IgnoreRuleIn, conn: Connection = Depends(get_archive_write_connection)) -> dict[str, Any]:
    """
    Add a rule: from the next message on, a plain-text message containing `pattern` (ignoring case) is neither archived nor edit-tracked,
    in every chat (`all_chats`) or in the chats listed in `chat_ids`.
    Already-archived messages are untouched.

        404 "chat_not_found"       - a listed chat has never been seen by the archive
                                     (`chat_ids` may hold an old id of a group that became a supergroup; it is stored under the current one)
        409 "ignore_rule_exists"   - a rule with the same phrase (ignoring case) already exists; remove it first to change where it applies
        409 "ignore_rules_full"    - the account is at IGNORE_RULE_MAX_COUNT rules
        422                        - an empty/over-long phrase, or both/neither of all_chats and chat_ids
    """
    with _needs_migrated_archive():
        if len(get_ignore_rules(conn)) >= IGNORE_RULE_MAX_COUNT:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"At most {IGNORE_RULE_MAX_COUNT} ignore rules are allowed. Remove one first.",
                    "reason": "ignore_rules_full",
                },
            )

        chats: dict[int, str | None] = {}
        for chat_id in body.chat_ids:
            canonical = db.queries.resolve_chat_id(conn, chat_id)
            chat = get_chat(conn, canonical)
            if chat is None:
                raise HTTPException(
                    status_code=404,
                    detail={"message": f"Chat {chat_id} is not in the archive, so a rule cannot be limited to it.", "reason": "chat_not_found"},
                )
            chats[canonical] = chat["name"]

        rule_id = db.queries.add_ignore_rule(conn, body.pattern, body.all_chats, list(chats.items()))
        if rule_id is None:
            raise HTTPException(
                status_code=409,
                detail={"message": "There is already an ignore rule with this text.", "reason": "ignore_rule_exists"},
            )
        return _ignore_rule_out(conn, rule_id)


@router.delete(
    "/ignore-rules/{rule_id}",
    response_model=IgnoreRuleRemovedOut,
    summary="Archive messages with that phrase again",
)
def remove_ignore_rule(
    rule_id: int = _RuleId,
    conn: Connection = Depends(get_archive_write_connection),
) -> dict[str, Any]:
    """Remove a rule: matching messages are archived again from the next one on (the ones ignored meanwhile were never stored).
    Idempotent - `removed` is False if there was no such rule."""
    with _needs_migrated_archive():
        removed = db.queries.remove_ignore_rule(conn, rule_id)
    return {"id": rule_id, "removed": removed}
