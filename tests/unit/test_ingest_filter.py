"""handlers/ingest_filter.py - the failure policy: when the blocklist cannot be read, the event is skipped, never archived on a guess."""

import asyncio
import logging

import pytest
from sqlalchemy.exc import OperationalError

from handlers import ingest_filter


def ask(chat_id=1):
    return asyncio.run(ingest_filter.should_skip(chat_id))


def test_a_blocked_chat_is_skipped(monkeypatch):
    monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: True)
    assert ask() is True


def test_an_unblocked_chat_is_not_skipped(monkeypatch):
    monkeypatch.setattr(ingest_filter, "_lookup", lambda chat_id: False)
    assert ask() is False


def test_an_unreachable_database_skips_with_one_short_line(monkeypatch, caplog):
    def down(chat_id):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(ingest_filter, "_lookup", down)
    with caplog.at_level(logging.ERROR, logger="handlers.ingest_filter"):
        assert ask(42) is True
    assert "database unreachable" in caplog.text and "42" in caplog.text
    assert "Traceback" not in caplog.text


def test_any_other_error_skips_and_keeps_the_traceback(monkeypatch, caplog):
    def boom(chat_id):
        raise ValueError("bug")

    monkeypatch.setattr(ingest_filter, "_lookup", boom)
    with caplog.at_level(logging.ERROR, logger="handlers.ingest_filter"):
        assert ask(42) is True
    assert "rather than risk archiving a blocked chat" in caplog.text
    assert any(r.exc_info for r in caplog.records)


def test_it_never_raises_even_before_the_database_is_initialised():
    """The real lookup, with no engine at all (db.init_db() was never called): still a plain 'skip', not an exception into Telethon's dispatcher."""
    import db

    db.close_db()
    assert ask() is True


def test_the_lookup_runs_off_the_event_loop(monkeypatch):
    """It is a blocking network round-trip - it must be handed to a worker thread (see handlers/on_message.py's threading note)."""
    import threading

    seen = {}

    def lookup(chat_id):
        seen["thread"] = threading.current_thread()
        return False

    monkeypatch.setattr(ingest_filter, "_lookup", lookup)
    ask()
    assert seen["thread"] is not threading.main_thread()


# ---------------------------------------------------------------------------
# Message ignore rules
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from telethon import types  # noqa: E402

from utils.text_matching import fold  # noqa: E402


def message(text: str | None = "hello", media=None, action=None):
    return SimpleNamespace(id=1, message=text, media=media, action=action)


def ask_text(msg, chat_id=1):
    return asyncio.run(ingest_filter.should_ignore_text(chat_id, msg))


class TestMatchableText:
    def test_a_plain_message_has_matchable_text(self):
        assert ingest_filter.matchable_text(message("hello")) == "hello"

    @pytest.mark.parametrize("media", [types.MessageMediaPhoto(), types.MessageMediaDocument(), types.MessageMediaGeo(geo=types.GeoPointEmpty())])
    def test_a_caption_is_not_matchable(self, media):
        assert ingest_filter.matchable_text(message("a caption", media=media)) is None

    def test_a_link_preview_does_not_make_the_text_a_caption(self):
        preview = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1), force_large_media=False, force_small_media=False, manual=False, safe=True)
        assert ingest_filter.matchable_text(message("see https://example.com", media=preview)) == "see https://example.com"

    def test_a_service_message_is_not_matchable(self):
        """Even with text on it (nothing real puts text on one, but the contract is 'a synthesized label is never matched'), so the guard itself is pinned."""
        assert ingest_filter.matchable_text(message("a label", action=types.MessageActionChatCreate(title="x", users=[]))) is None

    def test_no_text_is_not_matchable(self):
        assert ingest_filter.matchable_text(message("")) is None
        assert ingest_filter.matchable_text(message(None)) is None

    def test_a_message_object_without_a_media_attribute_is_treated_as_plain(self):
        """backfill's fakes (and older Telethon objects) may not carry the attribute at all."""
        assert ingest_filter.matchable_text(SimpleNamespace(message="hi", action=None)) == "hi"


class TestShouldIgnoreText:
    def test_a_matching_message_is_ignored(self, monkeypatch):
        monkeypatch.setattr(ingest_filter, "_load_patterns", lambda chat_id: [fold("тривога")])
        assert ask_text(message("ПОВІТРЯНА ТРИВОГА")) is True

    def test_a_non_matching_message_is_not(self, monkeypatch):
        monkeypatch.setattr(ingest_filter, "_load_patterns", lambda chat_id: [fold("тривога")])
        assert ask_text(message("hello")) is False

    def test_a_message_with_nothing_to_match_never_touches_the_database(self, monkeypatch):
        monkeypatch.setattr(ingest_filter, "_load_patterns", lambda chat_id: pytest.fail("the database was asked"))
        assert ask_text(message("caption", media=types.MessageMediaPhoto())) is False
        assert ask_text(message("")) is False

    def test_an_unreachable_database_skips_with_one_short_line(self, monkeypatch, caplog):
        def down(chat_id):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(ingest_filter, "_load_patterns", down)
        with caplog.at_level(logging.ERROR, logger="handlers.ingest_filter"):
            assert ask_text(message("secret text"), chat_id=42) is True
        assert "database unreachable" in caplog.text and "42" in caplog.text
        assert "secret text" not in caplog.text and "Traceback" not in caplog.text

    def test_any_other_error_skips_and_keeps_the_traceback_without_the_text(self, monkeypatch, caplog):
        def boom(chat_id):
            raise ValueError("bug")

        monkeypatch.setattr(ingest_filter, "_load_patterns", boom)
        with caplog.at_level(logging.ERROR, logger="handlers.ingest_filter"):
            assert ask_text(message("secret text"), chat_id=42) is True
        assert any(r.exc_info for r in caplog.records)
        assert "secret text" not in caplog.text

    def test_it_never_raises_even_before_the_database_is_initialised(self):
        import db

        db.close_db()
        assert ask_text(message("hello")) is True

    def test_the_lookup_runs_off_the_event_loop(self, monkeypatch):
        import threading

        seen = {}

        def load(chat_id):
            seen["thread"] = threading.current_thread()
            return []

        monkeypatch.setattr(ingest_filter, "_load_patterns", load)
        ask_text(message("hello"))
        assert seen["thread"] is not threading.main_thread()
