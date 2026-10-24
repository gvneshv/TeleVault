"""The ingest blocklist API (api/routes/ingest.py): the owner's own list of chats that are not archived."""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api import dependencies
from api.routes import ingest
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name

BLOCKLIST = "/api/ingest/blocklist"


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def archive_scalar(name: str, sql: str, **params):
    """Run one statement on an archive through a throwaway connection - to look at (or change) what the API stored, independently of the API."""
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.scalar() if result.returns_rows else None
    finally:
        eng.dispose()


def block(client, token, chat_id):
    return client.put(f"{BLOCKLIST}/{chat_id}", headers=auth(token))


def unblock(client, token, chat_id):
    return client.delete(f"{BLOCKLIST}/{chat_id}", headers=auth(token))


def listing(client, token):
    resp = client.get(BLOCKLIST, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def world(client):
    """Admin + two users with their own archives; every chat holds one message so there is something a block must leave alone."""
    admin = make_admin(client)
    alice, _ = make_user(client, admin, "alice")
    bob, _ = make_user(client, admin, "bob")
    archives = {}
    for who, token, base in (("alice", alice, 200), ("bob", bob, 300)):
        archives[who] = provision(client, token)
        for offset in (0, 1):
            seed_archive_by_name(
                archives[who], chat_id=base + offset, name=f"{who}-chat{offset}", messages=[(1, 1, f"{who} message {offset}")]
            )
    return {"admin": admin, "alice": alice, "bob": bob, "archives": archives}


class TestBlocking:
    def test_the_list_starts_empty(self, client, world):
        assert listing(client, world["alice"]) == {"items": [], "total": 0, "max_count": ingest.INGEST_BLOCKLIST_MAX_COUNT}

    def test_a_known_chat_can_be_blocked_and_shows_up_with_its_name(self, client, world):
        resp = block(client, world["alice"], 200)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat_id"] == 200 and resp.json()["name"] == "alice-chat0"
        got = listing(client, world["alice"])
        assert got["total"] == 1 and got["items"][0]["chat_id"] == 200

    def test_blocking_twice_returns_the_same_rule(self, client, world):
        first = block(client, world["alice"], 200).json()
        second = block(client, world["alice"], 200)
        assert second.status_code == 200 and second.json() == first
        assert listing(client, world["alice"])["total"] == 1

    def test_an_unknown_chat_cannot_be_blocked(self, client, world):
        resp = block(client, world["alice"], 999)
        assert resp.status_code == 404
        assert resp.json()["detail"]["reason"] == "chat_not_found"
        assert listing(client, world["alice"])["total"] == 0

    def test_blocking_leaves_the_archived_messages_alone(self, client, world):
        before = archive_scalar(world["archives"]["alice"], "SELECT COUNT(*) FROM messages")
        block(client, world["alice"], 200)
        assert archive_scalar(world["archives"]["alice"], "SELECT COUNT(*) FROM messages") == before
        assert client.get("/api/chats/200/messages", headers=auth(world["alice"])).json()["total"] == 1

    def test_the_rule_is_what_the_workers_read(self, client, world):
        """The API and the archiver share nothing but the database - so the stored rule must be exactly what db.queries.is_chat_blocked() sees."""
        block(client, world["alice"], 200)
        with db.get_tenant_connection(world["archives"]["alice"]) as conn:
            assert q.is_chat_blocked(conn, 200) is True
            assert q.is_chat_blocked(conn, 201) is False

    def test_the_old_id_of_an_upgraded_group_blocks_the_current_chat(self, client, world):
        seed_archive_by_name(world["archives"]["alice"], chat_id=-1005, name="Book club", chat_type="supergroup")
        archive_scalar(world["archives"]["alice"], "INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (-5, -1005)")
        resp = block(client, world["alice"], -5)
        assert resp.status_code == 200 and resp.json()["chat_id"] == -1005
        assert [i["chat_id"] for i in listing(client, world["alice"])["items"]] == [-1005]


class TestUnblocking:
    def test_unblocking_removes_the_rule(self, client, world):
        block(client, world["alice"], 200)
        resp = unblock(client, world["alice"], 200)
        assert resp.status_code == 200 and resp.json() == {"chat_id": 200, "removed": True}
        assert listing(client, world["alice"])["total"] == 0

    def test_unblocking_an_unblocked_chat_is_harmless(self, client, world):
        assert unblock(client, world["alice"], 200).json() == {"chat_id": 200, "removed": False}

    def test_a_rule_can_be_removed_after_its_chat_is_gone(self, client, world):
        """The planned 'delete a chat's data' removes the chats row; the rule (and the way out of it) must survive that."""
        block(client, world["alice"], 200)
        archive_scalar(world["archives"]["alice"], "DELETE FROM messages WHERE chat_id = 200")
        archive_scalar(world["archives"]["alice"], "DELETE FROM chats WHERE chat_id = 200")
        got = listing(client, world["alice"])
        assert [(i["chat_id"], i["name"]) for i in got["items"]] == [(200, "alice-chat0")]
        assert block(client, world["alice"], 200).status_code == 200  # still idempotent, even though the chat row is gone
        assert unblock(client, world["alice"], 200).json()["removed"] is True


class TestInputLimits:
    @pytest.mark.parametrize("chat_id", [2**63, -(2**63) - 1, 10**30])
    def test_a_chat_id_outside_the_64_bit_range_is_a_422_not_a_500(self, client, world, chat_id):
        assert block(client, world["alice"], chat_id).status_code == 422
        assert unblock(client, world["alice"], chat_id).status_code == 422

    @pytest.mark.parametrize("chat_id", [2**63 - 1, -(2**63)])
    def test_the_range_boundaries_themselves_are_accepted(self, client, world, chat_id):
        assert unblock(client, world["alice"], chat_id).status_code == 200  # nothing there, but a well-formed request

    def test_a_non_numeric_chat_id_is_a_422(self, client, world):
        assert block(client, world["alice"], "abc").status_code == 422

    def test_the_number_of_rules_is_capped(self, client, world, monkeypatch):
        monkeypatch.setattr(ingest, "INGEST_BLOCKLIST_MAX_COUNT", 1)
        assert block(client, world["alice"], 200).status_code == 200
        full = block(client, world["alice"], 201)
        assert full.status_code == 409 and full.json()["detail"]["reason"] == "blocklist_full"
        assert block(client, world["alice"], 200).status_code == 200  # an existing rule is not "one more"
        assert unblock(client, world["alice"], 200).json()["removed"] is True
        assert block(client, world["alice"], 201).status_code == 200  # room again


class TestIsolationAndAccess:
    def test_each_account_has_its_own_blocklist(self, client, world):
        provision(client, world["admin"])
        block(client, world["alice"], 200)
        assert listing(client, world["bob"])["total"] == 0
        assert listing(client, world["admin"])["total"] == 0  # being the admin grants no view into anyone's list

    def test_one_account_cannot_block_or_unblock_through_anothers_chat_ids(self, client, world):
        block(client, world["alice"], 200)
        assert block(client, world["bob"], 200).status_code == 404  # alice's chat id means nothing in bob's archive
        assert unblock(client, world["bob"], 200).json()["removed"] is False
        assert [i["chat_id"] for i in listing(client, world["alice"])["items"]] == [200]  # alice's rule untouched

    @pytest.mark.parametrize("call", [lambda c: c.get(BLOCKLIST), lambda c: c.put(f"{BLOCKLIST}/1"), lambda c: c.delete(f"{BLOCKLIST}/1")])
    def test_a_signed_out_caller_is_refused(self, client, call):
        assert call(client).status_code == 401

    def test_reading_the_list_uses_the_read_only_connection(self):
        """Only the two routes that must write may use the write dependency - a read route that quietly gained it could modify an archive."""
        def dependency_of(fn):
            return inspect.signature(fn).parameters["conn"].default.dependency

        assert dependency_of(ingest.list_blocklist) is dependencies.get_archive_connection
        assert dependency_of(ingest.block_chat) is dependencies.get_archive_write_connection
        assert dependency_of(ingest.unblock_chat) is dependencies.get_archive_write_connection

    def test_an_account_without_an_archive_gets_the_usual_409(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")  # registered, never provisioned an archive
        for resp in (client.get(BLOCKLIST, headers=auth(token)), block(client, token, 1), unblock(client, token, 1)):
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"


class TestArchiveNotMigratedYet:
    """Nothing migrates an existing archive automatically - every route must say so plainly instead of failing with a 500."""

    @pytest.fixture
    def outdated(self, client, world):
        archive_scalar(world["archives"]["alice"], "DROP TABLE ingest_blocked_chats")
        return world

    def test_every_route_answers_409_archive_outdated(self, client, outdated):
        for resp in (
            client.get(BLOCKLIST, headers=auth(outdated["alice"])),
            block(client, outdated["alice"], 200),
            unblock(client, outdated["alice"], 200),
        ):
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["reason"] == "archive_outdated"

    def test_the_rest_of_the_api_keeps_working(self, client, outdated):
        assert client.get("/api/chats", headers=auth(outdated["alice"])).status_code == 200
