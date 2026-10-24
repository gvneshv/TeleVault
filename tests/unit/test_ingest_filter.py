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
