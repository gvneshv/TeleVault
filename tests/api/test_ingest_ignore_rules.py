"""The message ignore rules API (api/routes/ingest.py): the owner's own list of phrases that keep a message out of the archive."""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api import dependencies
from api.routes import ingest
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name
from utils.text_matching import matches_any
from utils.validation import IGNORE_RULE_MAX_CHATS, IGNORE_RULE_MAX_LENGTH

RULES = "/api/ingest/ignore-rules"


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


def add(client, token, pattern="повітряна тривога", all_chats=None, chat_ids=None):
    body: dict = {"pattern": pattern}
    if all_chats is not None:
        body["all_chats"] = all_chats
    if chat_ids is not None:
        body["chat_ids"] = chat_ids
    return client.post(RULES, headers=auth(token), json=body)


def remove(client, token, rule_id):
    return client.delete(f"{RULES}/{rule_id}", headers=auth(token))


def listing(client, token):
    resp = client.get(RULES, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def world(client):
    """Admin + two users with their own archives; every chat holds one message so there is something a rule must leave alone."""
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


class TestAddingRules:
    def test_the_list_starts_empty_and_carries_the_limits_the_form_needs(self, client, world):
        got = listing(client, world["alice"])
        assert got["items"] == [] and got["total"] == 0
        assert (got["max_count"], got["max_length"], got["max_chats"]) == (
            ingest.IGNORE_RULE_MAX_COUNT,
            IGNORE_RULE_MAX_LENGTH,
            IGNORE_RULE_MAX_CHATS,
        )

    def test_an_all_chats_rule_is_created_and_listed(self, client, world):
        resp = add(client, world["alice"], "повітряна тривога", all_chats=True)
        assert resp.status_code == 201, resp.text
        rule = resp.json()
        assert rule["pattern"] == "повітряна тривога" and rule["all_chats"] is True and rule["chats"] == []
        assert listing(client, world["alice"])["items"] == [rule]

    def test_a_rule_for_chosen_chats_stores_each_chats_name(self, client, world):
        resp = add(client, world["alice"], "тривога", chat_ids=[200, 201])
        assert resp.status_code == 201, resp.text
        rule = resp.json()
        assert rule["all_chats"] is False
        assert [(c["chat_id"], c["name"]) for c in rule["chats"]] == [(200, "alice-chat0"), (201, "alice-chat1")]

    def test_the_phrase_is_trimmed_but_otherwise_kept_as_typed(self, client, world):
        rule = add(client, world["alice"], "   Повітряна ТРИВОГА \n", all_chats=True).json()
        assert rule["pattern"] == "Повітряна ТРИВОГА"

    def test_a_repeated_chat_id_is_one_chat(self, client, world):
        rule = add(client, world["alice"], "x", chat_ids=[200, 200, 201, 200]).json()
        assert [c["chat_id"] for c in rule["chats"]] == [200, 201]

    def test_the_same_phrase_in_another_case_is_refused(self, client, world):
        assert add(client, world["alice"], "Тривога", all_chats=True).status_code == 201
        resp = add(client, world["alice"], "ТРИВОГА", chat_ids=[200])
        assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "ignore_rule_exists"
        assert listing(client, world["alice"])["total"] == 1

    def test_an_unknown_chat_cannot_be_listed_and_nothing_is_stored(self, client, world):
        resp = add(client, world["alice"], "тривога", chat_ids=[200, 999])
        assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "chat_not_found"
        assert listing(client, world["alice"])["total"] == 0

    def test_the_old_id_of_an_upgraded_group_is_stored_under_the_current_one(self, client, world):
        seed_archive_by_name(world["archives"]["alice"], chat_id=-1005, name="Book club", chat_type="supergroup")
        archive_scalar(world["archives"]["alice"], "INSERT INTO chat_migrations (old_chat_id, new_chat_id) VALUES (-5, -1005)")
        rule = add(client, world["alice"], "тривога", chat_ids=[-5]).json()
        assert [c["chat_id"] for c in rule["chats"]] == [-1005]

    def test_adding_a_rule_leaves_archived_messages_alone(self, client, world):
        archive_scalar(world["archives"]["alice"], "UPDATE messages SET text = 'повітряна тривога'")
        add(client, world["alice"], "повітряна тривога", all_chats=True)
        assert archive_scalar(world["archives"]["alice"], "SELECT COUNT(*) FROM messages") == 2
        assert client.get("/api/chats/200/messages", headers=auth(world["alice"])).json()["total"] == 1

    def test_the_stored_rule_is_exactly_what_the_workers_read(self, client, world):
        """The API and the archiver share nothing but the database - so what the API stores must be what db.queries.get_ignore_patterns() hands the matcher."""
        add(client, world["alice"], "Оголошена ПОВІТРЯНА тривога", chat_ids=[200])
        add(client, world["alice"], "everywhere", all_chats=True)
        with db.get_tenant_connection(world["archives"]["alice"]) as conn:
            alert_chat = q.get_ignore_patterns(conn, 200)
            other_chat = q.get_ignore_patterns(conn, 201)
        assert matches_any(alert_chat, "🚨 М. ХАРКІВ - ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА") is True
        assert matches_any(other_chat, "🚨 М. ХАРКІВ - ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА") is False
        assert matches_any(other_chat, "say EVERYWHERE") is True


class TestRemovingRules:
    def test_removing_a_rule(self, client, world):
        rule = add(client, world["alice"], "тривога", chat_ids=[200]).json()
        resp = remove(client, world["alice"], rule["id"])
        assert resp.status_code == 200 and resp.json() == {"id": rule["id"], "removed": True}
        assert listing(client, world["alice"])["total"] == 0
        assert archive_scalar(world["archives"]["alice"], "SELECT COUNT(*) FROM ingest_ignore_rule_chats") == 0

    def test_removing_a_missing_rule_is_harmless(self, client, world):
        assert remove(client, world["alice"], 12345).json() == {"id": 12345, "removed": False}

    def test_a_removed_phrase_can_be_added_again(self, client, world):
        rule = add(client, world["alice"], "тривога", all_chats=True).json()
        remove(client, world["alice"], rule["id"])
        assert add(client, world["alice"], "тривога", chat_ids=[200]).status_code == 201

    def test_a_rule_survives_and_can_be_removed_after_its_chat_is_gone(self, client, world):
        add(client, world["alice"], "тривога", chat_ids=[200])
        archive_scalar(world["archives"]["alice"], "DELETE FROM messages WHERE chat_id = 200")
        archive_scalar(world["archives"]["alice"], "DELETE FROM chats WHERE chat_id = 200")
        rule = listing(client, world["alice"])["items"][0]
        assert [(c["chat_id"], c["name"]) for c in rule["chats"]] == [(200, "alice-chat0")]
        assert remove(client, world["alice"], rule["id"]).json()["removed"] is True


class TestInputLimits:
    @pytest.mark.parametrize("pattern", ["", "   ", "\n\t "])
    def test_an_empty_phrase_is_a_422(self, client, world, pattern):
        assert add(client, world["alice"], pattern, all_chats=True).status_code == 422

    def test_the_longest_phrase_is_accepted_and_one_more_is_not(self, client, world):
        assert add(client, world["alice"], "я" * IGNORE_RULE_MAX_LENGTH, all_chats=True).status_code == 201
        assert add(client, world["alice"], "я" * (IGNORE_RULE_MAX_LENGTH + 1), all_chats=True).status_code == 422

    def test_the_length_limit_counts_the_trimmed_phrase(self, client, world):
        assert add(client, world["alice"], "  " + "я" * IGNORE_RULE_MAX_LENGTH + "  ", all_chats=True).status_code == 201

    def test_a_nul_character_is_a_422_not_a_500(self, client, world):
        assert add(client, world["alice"], "a\x00b", all_chats=True).status_code == 422

    @pytest.mark.parametrize(
        "all_chats, chat_ids",
        [(True, [200]), (False, []), (None, None), (False, None)],
        ids=["both", "empty-list", "neither-unset", "neither-explicit"],
    )
    def test_choosing_both_or_neither_scope_is_a_422(self, client, world, all_chats, chat_ids):
        assert add(client, world["alice"], "тривога", all_chats=all_chats, chat_ids=chat_ids).status_code == 422

    def test_too_many_chats_in_one_rule_is_a_422(self, client, world):
        resp = add(client, world["alice"], "тривога", chat_ids=list(range(1, IGNORE_RULE_MAX_CHATS + 2)))
        assert resp.status_code == 422

    @pytest.mark.parametrize("chat_id", [2**63, -(2**63) - 1, 10**30])
    def test_a_chat_id_outside_the_64_bit_range_is_a_422_not_a_500(self, client, world, chat_id):
        assert add(client, world["alice"], "тривога", chat_ids=[chat_id]).status_code == 422

    def test_a_non_numeric_chat_id_is_a_422(self, client, world):
        resp = client.post(RULES, headers=auth(world["alice"]), json={"pattern": "x", "chat_ids": ["abc"]})
        assert resp.status_code == 422

    def test_the_rule_id_in_the_path_is_bounded(self, client, world):
        assert remove(client, world["alice"], 0).status_code == 422
        assert remove(client, world["alice"], 2**63).status_code == 422
        assert remove(client, world["alice"], "abc").status_code == 422

    def test_a_missing_body_is_a_422(self, client, world):
        assert client.post(RULES, headers=auth(world["alice"])).status_code == 422

    def test_the_number_of_rules_is_capped(self, client, world, monkeypatch):
        monkeypatch.setattr(ingest, "IGNORE_RULE_MAX_COUNT", 1)
        first = add(client, world["alice"], "one", all_chats=True)
        assert first.status_code == 201
        full = add(client, world["alice"], "two", all_chats=True)
        assert full.status_code == 409 and full.json()["detail"]["reason"] == "ignore_rules_full"
        assert remove(client, world["alice"], first.json()["id"]).json()["removed"] is True
        assert add(client, world["alice"], "two", all_chats=True).status_code == 201  # room again


class TestIsolationAndAccess:
    def test_each_account_has_its_own_rules(self, client, world):
        provision(client, world["admin"])
        add(client, world["alice"], "тривога", all_chats=True)
        assert listing(client, world["bob"])["total"] == 0
        assert listing(client, world["admin"])["total"] == 0  # being the admin grants no view into anyone's list

    def test_one_account_cannot_limit_a_rule_to_or_remove_anothers(self, client, world):
        rule = add(client, world["alice"], "тривога", chat_ids=[200]).json()
        assert add(client, world["bob"], "тривога", chat_ids=[200]).status_code == 404  # alice's chat id means nothing in bob's archive
        assert remove(client, world["bob"], rule["id"]).json()["removed"] is False
        assert [r["id"] for r in listing(client, world["alice"])["items"]] == [rule["id"]]  # alice's rule untouched

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.get(RULES),
            lambda c: c.post(RULES, json={"pattern": "x", "all_chats": True}),
            lambda c: c.delete(f"{RULES}/1"),
        ],
    )
    def test_a_signed_out_caller_is_refused(self, client, call):
        assert call(client).status_code == 401

    def test_only_the_routes_that_must_write_use_the_write_connection(self):
        def dependency_of(fn):
            return inspect.signature(fn).parameters["conn"].default.dependency

        assert dependency_of(ingest.list_ignore_rules) is dependencies.get_archive_connection
        assert dependency_of(ingest.add_ignore_rule) is dependencies.get_archive_write_connection
        assert dependency_of(ingest.remove_ignore_rule) is dependencies.get_archive_write_connection

    def test_an_account_without_an_archive_gets_the_usual_409(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "carol")  # registered, never provisioned an archive
        for resp in (client.get(RULES, headers=auth(token)), add(client, token, "x", all_chats=True), remove(client, token, 1)):
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_unattached"


class TestNothingIsLogged:
    def test_a_phrase_never_reaches_the_log(self, client, world, caplog):
        with caplog.at_level("DEBUG"):
            add(client, world["alice"], "СЕКРЕТНА ФРАЗА", all_chats=True)
            add(client, world["alice"], "секретна фраза", all_chats=True)  # the duplicate path
            listing(client, world["alice"])
        assert "секретна" not in caplog.text.lower()


class TestArchiveNotMigratedYet:
    """Nothing migrates an existing archive automatically - every route must say so plainly instead of failing with a 500."""

    @pytest.fixture
    def outdated(self, client, world):
        archive_scalar(world["archives"]["alice"], "DROP TABLE ingest_ignore_rule_chats")
        archive_scalar(world["archives"]["alice"], "DROP TABLE ingest_ignore_rules")
        return world

    def test_every_route_answers_409_archive_outdated(self, client, outdated):
        for resp in (
            client.get(RULES, headers=auth(outdated["alice"])),
            add(client, outdated["alice"], "x", all_chats=True),
            add(client, outdated["alice"], "x", chat_ids=[200]),
            remove(client, outdated["alice"], 1),
        ):
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["reason"] == "archive_outdated"

    def test_the_blocklist_and_the_rest_of_the_api_keep_working(self, client, outdated):
        assert client.get("/api/ingest/blocklist", headers=auth(outdated["alice"])).status_code == 200
        assert client.get("/api/chats", headers=auth(outdated["alice"])).status_code == 200
