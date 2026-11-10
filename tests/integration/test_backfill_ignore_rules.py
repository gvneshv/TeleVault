"""
backfill.py and the message ignore rules, end to end: the REAL run() against a real, migrated archive database, with only Telegram faked.
Reuses the fake Telegram and the `telegram` fixture of test_backfill_blocklist.py.
"""

import logging

import sqlalchemy as sa
from telethon import types

import db
import db.queries as q
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (a fixture imported to be used)


def texts(conn, chat_id):
    conn.rollback()  # see the rows backfill.run() committed from its own connection
    return [r[0] for r in conn.execute(sa.text("SELECT text FROM messages WHERE chat_id = :c ORDER BY tg_message_id"), {"c": chat_id})]


def test_a_matching_message_is_not_stored_and_the_rest_of_the_chat_is(archive, telegram):  # noqa: F811
    q.add_ignore_rule(archive.conn, "повітряна тривога", all_chats=True)
    _, status = telegram({11: ["Привіт", "🚨 ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА", "Як справи?"]})
    assert texts(archive.conn, 11) == ["Привіт", "Як справи?"]
    assert status["state"] == "completed"


def test_without_any_rule_everything_is_stored(archive, telegram):  # noqa: F811
    telegram({11: ["Привіт", "повітряна тривога"]})
    assert stored(archive.conn) == {11: 2}


def test_a_rule_limited_to_one_chat_leaves_the_others_alone(archive, telegram):  # noqa: F811
    q.add_ignore_rule(archive.conn, "тривога", all_chats=False, chats=[(11, "alerts")])
    telegram({11: ["тривога"], 12: ["знову тривога у друга"]})
    assert stored(archive.conn) == {12: 1}


def test_a_caption_is_never_matched(archive, telegram):  # noqa: F811
    q.add_ignore_rule(archive.conn, "тривога", all_chats=True)
    telegram({11: [("фото: тривога", types.MessageMediaPhoto()), "тривога"]})
    assert texts(archive.conn, 11) == ["фото: тривога"]


def test_a_message_already_stored_is_not_removed_by_a_later_backfill(archive, telegram):  # noqa: F811
    """Rules apply to what is fetched, never to what the archive already holds."""
    telegram({11: ["тривога"]})
    q.add_ignore_rule(archive.conn, "тривога", all_chats=True)
    telegram({11: ["тривога"]})
    assert texts(archive.conn, 11) == ["тривога"]


def test_unreadable_rules_fail_that_chat_instead_of_archiving_it(archive, telegram, monkeypatch, caplog):  # noqa: F811
    def broken(conn, chat_id):
        raise RuntimeError("rules unreadable")

    monkeypatch.setattr(db.queries, "get_ignore_patterns", broken)
    with caplog.at_level(logging.ERROR, logger="backfill"):
        _, status = telegram({11: ["maybe ignored"]})
    assert stored(archive.conn) == {}
    assert "Failed to backfill" in caplog.text
    assert status["state"] == "error"


def test_neither_the_phrase_nor_the_text_is_logged(archive, telegram, caplog):  # noqa: F811
    q.add_ignore_rule(archive.conn, "секретна фраза", all_chats=True)
    with caplog.at_level(logging.DEBUG):
        telegram({11: ["тут СЕКРЕТНА ФРАЗА є"]})
    assert "секретна" not in caplog.text.lower()
