"""
backfill.py and a chat whose Telegram account was deleted.

Telegram keeps such a user in the dialog list but strips the profile: the entity comes back with `deleted=True` and an empty (or missing) first name.
The archive must keep the name it already knows - blanking it would turn a readable chat into a bare number in every picker.
"""

import asyncio

import pytest
import sqlalchemy as sa
from telethon import types

import backfill
import db.queries as q


class _NoHistory:
    """A client whose chat has no messages left to fetch - only the chat bookkeeping at the start of backfill_chat() is under test."""

    async def iter_messages(self, *args, **kwargs):
        return
        yield  # pragma: no cover - makes this an (empty) async generator


def chat_name(conn, chat_id: int):
    return conn.execute(sa.text("SELECT name FROM chats WHERE chat_id = :c"), {"c": chat_id}).scalar()


@pytest.mark.parametrize("first_name", ["", None])
def test_a_deleted_account_never_blanks_the_name_already_stored(archive, first_name):
    q.upsert_chat(archive.conn, chat_id=555, name="Maria", chat_type="private")
    deleted_user = types.User(id=555, first_name=first_name, deleted=True, access_hash=1)

    asyncio.run(backfill.backfill_chat(_NoHistory(), archive.conn, deleted_user, limit=None))  # type: ignore[arg-type]

    assert chat_name(archive.conn, 555) == "Maria"


def test_a_live_rename_is_still_followed(archive):
    """The guard must only ignore EMPTY names; a real rename keeps working."""
    q.upsert_chat(archive.conn, chat_id=556, name="Old", chat_type="private")
    asyncio.run(backfill.backfill_chat(_NoHistory(), archive.conn, types.User(id=556, first_name="New", access_hash=1), limit=None))  # type: ignore[arg-type]
    assert chat_name(archive.conn, 556) == "New"