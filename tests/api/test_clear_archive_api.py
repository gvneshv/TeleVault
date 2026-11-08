"""DELETE /api/archive/data - forget everything stored, keep the owner's settings (api/routes/archive.py)."""

import inspect
import json
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api import dependencies
from api.routes import archive
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name
from utils.worker_paths import backfill_status_path

URL = "/api/archive/data"
CONFIRM = {"confirm": "delete-all-data"}


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def on_archive(name: str, fn):
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            return fn(conn)
    finally:
        eng.dispose()


def count(name: str, table: str) -> int:
    return on_archive(name, lambda c: c.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar())


def clear(client, token, **params):
    return client.delete(URL, headers=auth(token), params=params if params else CONFIRM)


@pytest.fixture
def world(client):
    """Alice and Bob, each with an archive holding a chat of 3 messages by sender 5; Alice also has a label, a block rule and an owner id."""
    admin = make_admin(client)
    alice, alice_id = make_user(client, admin, "alice")
    bob, _ = make_user(client, admin, "bob")
    archives = {}
    for who, token, base in (("alice", alice, 200), ("bob", bob, 300)):
        archives[who] = provision(client, token)
        seed_archive_by_name(archives[who], chat_id=base, name=f"{who}-chat", messages=[(1, 5, "a"), (2, 5, "b"), (3, 6, "c")])

    def settings(conn):
        q.set_sender_display_name(conn, 5, "Mum")
        q.block_chat(conn, 999, "Blocked")
        q.record_archive_owner(conn, 4242)

    on_archive(archives["alice"], settings)
    return {"alice": alice, "alice_id": alice_id, "bob": bob, "archives": archives}


class TestClearing:
    def test_everything_stored_is_removed_and_the_counts_are_returned(self, client, world):
        resp = clear(client, world["alice"])
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "messages_deleted": 3, "edits_deleted": 0, "deletions_deleted": 0, "chats_deleted": 1,
            "senders_deleted": 1, "senders_kept": 1, "backfill_runs_deleted": 0,
        }
        name = world["archives"]["alice"]
        assert count(name, "messages") == 0 and count(name, "chats") == 0
        assert client.get("/api/chats", headers=auth(world["alice"])).json()["total"] == 0

    def test_the_settings_stay(self, client, world):
        clear(client, world["alice"])
        assert client.get("/api/senders/display-names", headers=auth(world["alice"])).json()["items"] == [{"sender_id": 5, "display_name": "Mum"}]
        assert client.get("/api/ingest/blocklist", headers=auth(world["alice"])).json()["total"] == 1
        assert client.get("/api/archive/owner", headers=auth(world["alice"])).json()["owner_id"] == 4242

    def test_clearing_twice_is_fine(self, client, world):
        assert clear(client, world["alice"]).status_code == 200
        second = clear(client, world["alice"])
        assert second.status_code == 200 and second.json()["messages_deleted"] == 0

    def test_another_accounts_archive_is_untouched(self, client, world):
        clear(client, world["alice"])
        assert count(world["archives"]["bob"], "messages") == 3 and count(world["archives"]["bob"], "chats") == 1


class TestConfirmation:
    @pytest.mark.parametrize("params", [{}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "DELETE-ALL-DATA"}, {"confirm": "delete-all-data "}, {"confirm": "x" * 200}])
    def test_without_the_exact_phrase_nothing_is_touched(self, client, world, params):
        resp = client.delete(URL, headers=auth(world["alice"]), params=params)
        assert resp.status_code == 422, resp.text
        assert count(world["archives"]["alice"], "messages") == 3

    def test_a_wrong_phrase_says_why(self, client, world):
        resp = client.delete(URL, headers=auth(world["alice"]), params={"confirm": "yes"})
        assert resp.json()["detail"]["reason"] == "confirmation_required"


class TestRefusals:
    def test_it_is_refused_while_a_backfill_is_running_and_nothing_is_deleted(self, client, world):
        path = backfill_status_path(world["alice_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))  # this very process is a live pid
        try:
            resp = clear(client, world["alice"])
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "backfill_running"
            assert count(world["archives"]["alice"], "messages") == 3
        finally:
            path.unlink()
        assert clear(client, world["alice"]).status_code == 200  # the same request goes through once the backfill is over

    def test_a_signed_out_caller_is_refused(self, client):
        assert client.delete(URL, params=CONFIRM).status_code == 401

    def test_an_account_without_an_archive_gets_the_usual_409(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")
        resp = clear(client, token)
        assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"

    def test_the_route_uses_the_write_connection(self):
        dep = inspect.signature(archive.clear_archive_data).parameters["conn"].default.dependency
        assert dep is dependencies.get_archive_write_connection