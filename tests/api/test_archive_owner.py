"""GET /api/archive/owner (api/routes/archive.py): which Telegram account an archive belongs to, so the chat view can put its messages on the right."""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api import dependencies
from api.routes import archive as archive_routes
from tests.helpers import auth, make_admin, make_user

OWNER = "/api/archive/owner"


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def on_archive(name: str, fn):
    """Run `fn(connection)` on an archive through a throwaway connection - to look at or change what the API reads, independently of the API."""
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            return fn(conn)
    finally:
        eng.dispose()


@pytest.fixture
def world(client):
    admin = make_admin(client)
    alice, _ = make_user(client, admin, "alice")
    bob, _ = make_user(client, admin, "bob")
    return {"alice": alice, "bob": bob, "archives": {"alice": provision(client, alice), "bob": provision(client, bob)}}


def test_the_owner_is_null_until_the_archiver_has_recorded_it(client, world):
    resp = client.get(OWNER, headers=auth(world["alice"]))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"owner_id": None}


def test_the_recorded_owner_is_returned(client, world):
    on_archive(world["archives"]["alice"], lambda c: q.record_archive_owner(c, 5_000_000_001))
    assert client.get(OWNER, headers=auth(world["alice"])).json() == {"owner_id": 5_000_000_001}


def test_each_account_sees_only_its_own_owner(client, world):
    on_archive(world["archives"]["alice"], lambda c: q.record_archive_owner(c, 111))
    on_archive(world["archives"]["bob"], lambda c: q.record_archive_owner(c, 222))
    assert client.get(OWNER, headers=auth(world["alice"])).json()["owner_id"] == 111
    assert client.get(OWNER, headers=auth(world["bob"])).json()["owner_id"] == 222


def test_an_archive_that_predates_the_migration_answers_null_not_an_error(client, world):
    """Nothing migrates existing archives automatically: the chat view must keep working, just without the right-hand side."""
    on_archive(world["archives"]["alice"], lambda c: (c.execute(sa.text("DROP TABLE archive_owner")), c.commit()))
    resp = client.get(OWNER, headers=auth(world["alice"]))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"owner_id": None}
    assert client.get("/api/chats", headers=auth(world["alice"])).status_code == 200  # and the connection pool is not poisoned


def test_a_signed_out_caller_is_refused(client):
    assert client.get(OWNER).status_code == 401


def test_an_account_without_an_archive_gets_the_usual_409(client):
    admin = make_admin(client)
    token, _ = make_user(client, admin, "carol")
    resp = client.get(OWNER, headers=auth(token))
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"


def test_it_only_reads(client):
    """A GET must never hold the write connection."""
    assert inspect.signature(archive_routes.get_owner).parameters["conn"].default.dependency is dependencies.get_archive_connection
