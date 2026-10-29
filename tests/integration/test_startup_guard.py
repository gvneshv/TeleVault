"""api/server.py's first-admin guard, against a real control database."""

import dataclasses
import logging

import pytest

import api.server as server
import control_db
import control_db.connection  # the submodule itself: `control_db` only re-exports a few names, and the pool check below reads get_engine()
from config import settings
from control_db import queries as cq


@pytest.fixture
def control_pool(control_engine):
    """The app-level control pool that _ensure_first_admin_is_possible() uses (the API lifespan opens it in production)."""
    control_db.init_control_db(settings.control_database_url)
    yield
    control_db.close_db()


def _with_token(monkeypatch, token):
    monkeypatch.setattr(server, "settings", dataclasses.replace(settings, bootstrap_admin_token=token))


def test_refuses_when_there_is_no_admin_and_no_token(control_pool, monkeypatch):
    _with_token(monkeypatch, None)
    with pytest.raises(RuntimeError) as exc:
        server._ensure_first_admin_is_possible()
    message = str(exc.value)
    assert "TELEVAULT_BOOTSTRAP_ADMIN_TOKEN" in message
    assert "scripts/manage_admin.py create" in message  # both remedies are spelled out


def test_allows_start_when_a_token_is_configured(control_pool, monkeypatch):
    _with_token(monkeypatch, "some-phrase")
    server._ensure_first_admin_is_possible()


def test_allows_start_without_a_token_once_an_admin_exists(control_pool, control_conn, monkeypatch):
    cq.create_admin_user(control_conn, "root", "hash")
    _with_token(monkeypatch, None)
    server._ensure_first_admin_is_possible()


def test_an_unreachable_database_only_warns_and_does_not_block_startup(monkeypatch, caplog):
    control_db.init_control_db("postgresql+psycopg://televault:wrong@127.0.0.1:1/nope")
    try:
        _with_token(monkeypatch, None)
        with caplog.at_level(logging.WARNING, logger="api.server"):
            server._ensure_first_admin_is_possible()
    finally:
        control_db.close_db()
    assert "Could not check whether an admin account exists" in caplog.text
    assert "Traceback" not in caplog.text


def test_the_real_lifespan_aborts_startup_and_releases_its_pools(control_engine, monkeypatch):
    from fastapi.testclient import TestClient

    _with_token(monkeypatch, None)
    with pytest.raises(RuntimeError, match="nobody would be able to register"):
        with TestClient(server.app):
            pass
    with pytest.raises(RuntimeError):  # pools were closed on the way out
        control_db.connection.get_engine()