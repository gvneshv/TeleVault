"""The senders API (api/routes/senders.py): the owner's display names and the name history of a sender."""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api import dependencies
from api.routes import senders
from utils.validation import DISPLAY_NAME_MAX_LENGTH
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name

SENDERS = "/api/senders"


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def archive_exec(name: str, sql: str, **params):
    """Run one statement on an archive through a throwaway connection, independently of the API."""
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.scalar() if result.returns_rows else None
    finally:
        eng.dispose()


def rename_in_archive(name: str, sender_id: int, **fields):
    """What the archiver does when it sees a sender under a new name."""
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            q.upsert_sender(conn, sender_id=sender_id, username=fields.get("username", f"user{sender_id}"),
                            first_name=fields.get("first_name", f"User{sender_id}"), last_name=fields.get("last_name"))
    finally:
        eng.dispose()


def put(client, token, sender_id, name):
    return client.put(f"{SENDERS}/{sender_id}/display-name", headers=auth(token), json={"display_name": name})


def delete(client, token, sender_id):
    return client.delete(f"{SENDERS}/{sender_id}/display-name", headers=auth(token))


def listing(client, token):
    resp = client.get(f"{SENDERS}/display-names", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def world(client):
    """Admin + two users with their own archives; sender 5 exists in both, with different messages."""
    admin = make_admin(client)
    alice, _ = make_user(client, admin, "alice")
    bob, _ = make_user(client, admin, "bob")
    archives = {}
    for who, token, base in (("alice", alice, 200), ("bob", bob, 300)):
        archives[who] = provision(client, token)
        seed_archive_by_name(archives[who], chat_id=base, name=f"{who}-chat", messages=[(1, 5, f"{who} hello")])
    return {"admin": admin, "alice": alice, "bob": bob, "archives": archives}


class TestDisplayNames:
    def test_the_list_starts_empty(self, client, world):
        assert listing(client, world["alice"]) == {"items": [], "total": 0, "max_count": senders.DISPLAY_NAME_MAX_COUNT}

    def test_a_label_can_be_set_listed_changed_and_removed(self, client, world):
        resp = put(client, world["alice"], 5, "Mum")
        assert resp.status_code == 200, resp.text
        assert resp.json()["sender_id"] == 5 and resp.json()["display_name"] == "Mum"
        assert listing(client, world["alice"])["items"] == [{"sender_id": 5, "display_name": "Mum"}]
        assert put(client, world["alice"], 5, "Mom").json()["display_name"] == "Mom"
        assert listing(client, world["alice"])["total"] == 1  # a change replaces, never adds
        assert delete(client, world["alice"], 5).json() == {"sender_id": 5, "removed": True}
        assert delete(client, world["alice"], 5).json() == {"sender_id": 5, "removed": False}  # idempotent
        assert listing(client, world["alice"])["total"] == 0

    def test_the_text_is_trimmed(self, client, world):
        assert put(client, world["alice"], 5, "  Mum  ").json()["display_name"] == "Mum"

    @pytest.mark.parametrize("bad", ["", "   ", "a\nb", "a\tb", "a\x00b", "x" * (DISPLAY_NAME_MAX_LENGTH + 1)])
    def test_an_empty_too_long_or_control_character_label_is_a_422(self, client, world, bad):
        assert put(client, world["alice"], 5, bad).status_code == 422
        assert listing(client, world["alice"])["total"] == 0

    def test_the_longest_allowed_label_and_emoji_are_accepted(self, client, world):
        assert put(client, world["alice"], 5, "я" * DISPLAY_NAME_MAX_LENGTH).status_code == 200
        assert put(client, world["alice"], 5, "👨‍👩‍👧 Family").status_code == 200  # the zero-width joiner is not a control character

    def test_a_missing_or_wrong_typed_body_is_a_422(self, client, world):
        assert client.put(f"{SENDERS}/5/display-name", headers=auth(world["alice"]), json={}).status_code == 422
        assert client.put(f"{SENDERS}/5/display-name", headers=auth(world["alice"]), json={"display_name": 5}).status_code == 422

    def test_an_unknown_sender_is_404_and_stores_nothing(self, client, world):
        resp = put(client, world["alice"], 999, "Ghost")
        assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "sender_not_found"
        assert listing(client, world["alice"])["total"] == 0

    @pytest.mark.parametrize("sender_id", [2**63, -(2**63) - 1])
    def test_an_out_of_range_sender_id_is_a_422_not_a_database_error(self, client, world, sender_id):
        assert put(client, world["alice"], sender_id, "x").status_code == 422
        assert client.get(f"{SENDERS}/{sender_id}", headers=auth(world["alice"])).status_code == 422

    def test_a_non_numeric_id_is_a_422_and_does_not_swallow_the_list_route(self, client, world):
        assert client.get(f"{SENDERS}/abc", headers=auth(world["alice"])).status_code == 422
        assert client.get(f"{SENDERS}/display-names", headers=auth(world["alice"])).status_code == 200

    def test_the_label_limit_refuses_a_new_label_but_not_a_change(self, client, world, monkeypatch):
        monkeypatch.setattr(senders, "DISPLAY_NAME_MAX_COUNT", 1)
        archive = world["archives"]["alice"]
        seed_archive_by_name(archive, chat_id=201, name="more", messages=[(1, 6, "hi")])
        assert put(client, world["alice"], 5, "Mum").status_code == 200
        full = put(client, world["alice"], 6, "Dad")
        assert full.status_code == 409 and full.json()["detail"]["reason"] == "display_names_full"
        assert put(client, world["alice"], 5, "Mom").status_code == 200  # changing an existing one never needs room
        assert delete(client, world["alice"], 5).status_code == 200
        assert put(client, world["alice"], 6, "Dad").status_code == 200  # room again

    def test_messages_still_carry_the_telegram_name_not_the_label(self, client, world):
        """The UI applies labels itself; the message endpoints, their search and pagination do not change."""
        put(client, world["alice"], 5, "Mum")
        msg = client.get("/api/messages", headers=auth(world["alice"])).json()["items"][0]
        assert msg["sender"]["display_name"] is None


class TestSenderDetail:
    def test_a_sender_who_never_changed(self, client, world):
        resp = client.get(f"{SENDERS}/5", headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sender_id"] == 5 and body["display_name"] is None
        assert [(h["username"], h["first_name"]) for h in body["history"]] == [("user5", "User5")]

    def test_the_history_lists_what_the_archiver_saw_newest_first(self, client, world):
        rename_in_archive(world["archives"]["alice"], 5, username="mum_new", first_name="Mum")
        rename_in_archive(world["archives"]["alice"], 5, username="mum_new", first_name="Mama")
        put(client, world["alice"], 5, "Mum")
        body = client.get(f"{SENDERS}/5", headers=auth(world["alice"])).json()
        assert [(h["username"], h["first_name"]) for h in body["history"]] == [("mum_new", "Mama"), ("mum_new", "Mum"), ("user5", "User5")]
        assert body["first_name"] == "User5"  # the plain fields stay the first-seen identity
        assert body["display_name"] == "Mum" and body["display_name_updated_at"] is not None

    def test_an_unknown_sender_is_404(self, client, world):
        resp = client.get(f"{SENDERS}/999", headers=auth(world["alice"]))
        assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "sender_not_found"


class TestIsolationAndAccess:
    def test_each_account_has_its_own_labels_and_history(self, client, world):
        put(client, world["alice"], 5, "Mum")
        rename_in_archive(world["archives"]["alice"], 5, first_name="Changed")
        assert listing(client, world["bob"])["total"] == 0
        assert len(client.get(f"{SENDERS}/5", headers=auth(world["bob"])).json()["history"]) == 1

    def test_one_account_cannot_change_anothers_labels(self, client, world):
        put(client, world["alice"], 5, "Mum")
        assert put(client, world["bob"], 5, "Hijack").json()["display_name"] == "Hijack"  # bob's own sender 5, a different person
        assert delete(client, world["bob"], 5).json()["removed"] is True
        assert listing(client, world["alice"])["items"] == [{"sender_id": 5, "display_name": "Mum"}]

    @pytest.mark.parametrize(
        "call",
        [lambda c: c.get(f"{SENDERS}/display-names"), lambda c: c.get(f"{SENDERS}/5"),
         lambda c: c.put(f"{SENDERS}/5/display-name", json={"display_name": "x"}), lambda c: c.delete(f"{SENDERS}/5/display-name")],
    )
    def test_a_signed_out_caller_is_refused(self, client, call):
        assert call(client).status_code == 401

    def test_reads_use_the_read_only_connection_and_only_the_writes_use_the_write_one(self):
        def dependency_of(fn):
            return inspect.signature(fn).parameters["conn"].default.dependency

        assert dependency_of(senders.list_display_names) is dependencies.get_archive_connection
        assert dependency_of(senders.get_sender) is dependencies.get_archive_connection
        assert dependency_of(senders.set_display_name) is dependencies.get_archive_write_connection
        assert dependency_of(senders.remove_display_name) is dependencies.get_archive_write_connection

    def test_an_account_without_an_archive_gets_the_usual_409(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")
        for resp in (client.get(f"{SENDERS}/display-names", headers=auth(token)), put(client, token, 1, "x"), client.get(f"{SENDERS}/1", headers=auth(token))):
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"


class TestArchiveNotMigratedYet:
    """Nothing migrates an existing archive automatically: reads stay quiet, writes say so plainly instead of a 500."""

    @pytest.fixture
    def outdated(self, client, world):
        archive_exec(world["archives"]["alice"], "DROP TABLE sender_display_names")
        archive_exec(world["archives"]["alice"], "DROP TABLE sender_name_history")
        return world

    def test_reads_answer_as_if_there_were_no_labels_and_no_history(self, client, outdated):
        assert listing(client, outdated["alice"])["items"] == []
        body = client.get(f"{SENDERS}/5", headers=auth(outdated["alice"])).json()
        assert body["display_name"] is None and len(body["history"]) == 1

    def test_writes_answer_409_archive_outdated(self, client, outdated):
        for resp in (put(client, outdated["alice"], 5, "Mum"), delete(client, outdated["alice"], 5)):
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["reason"] == "archive_outdated"

    def test_the_rest_of_the_api_keeps_working(self, client, outdated):
        assert client.get("/api/chats", headers=auth(outdated["alice"])).status_code == 200
