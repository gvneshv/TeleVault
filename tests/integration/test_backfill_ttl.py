"""
backfill.py and self-destructing messages, end to end against a real archive database (only Telegram is faked, see test_backfill_blocklist.py).

Telegram's API Terms forbid keeping self-destructing content from disappearing, so history that still carries an auto-delete timer or view-once media is not stored.
"""

from telethon import types

# The wiring fixture and the fake client are shared with the blocklist tests; importing a fixture by name is how pytest shares one across modules.
from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401


def test_messages_with_an_auto_delete_timer_are_not_archived(archive, telegram):  # noqa: F811
    fake, status = telegram({11: ["kept", ("timed", None, 86400), "kept too"]})
    assert stored(archive.conn) == {11: 2}
    assert status["state"] == "completed"


def test_a_view_once_caption_is_not_archived(archive, telegram):  # noqa: F811
    telegram({11: [("a caption", types.MessageMediaPhoto(ttl_seconds=10)), ("plain caption", types.MessageMediaPhoto())]})
    # The second one is ordinary media with a caption: archived as before.
    # Only the timed one is dropped.
    assert stored(archive.conn) == {11: 1}


def test_a_chat_made_only_of_timed_messages_stores_nothing(archive, telegram):  # noqa: F811
    telegram({11: [("a", None, 60), ("b", None, 3600)]})
    assert stored(archive.conn) == {}
