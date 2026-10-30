"""
The HTTP surface of the chat view (docs: claude/chat-view-spec.md, step A):

    - `edit_count` on message rows and `edited_count` on chats,
    - GET    /api/chats/{chat_id}/window   the anchored, cursor-based window of a conversation,
    - DELETE /api/chats/{chat_id}          remove everything stored for one chat.

The queries themselves are covered in tests/integration/test_chat_view_queries.py; this file is about what a CLIENT can rely on:
status codes, bounds on every parameter, and that one account can never reach another account's archive through any of it.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from api.routes import chats as chats_routes
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name

CHATS = "/api/chats"


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def archive_scalar(name: str, sql: str, **params):
    """One statement on an archive through a throwaway connection - to look at what the API did, independently of the API."""
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.scalar() if result.returns_rows else None
    finally:
        eng.dispose()


def archive_edit_twice(name: str, chat_id: int, tg_message_id: int) -> None:
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            for text in ("second version", "third version"):
                q.record_edit(conn, tg_message_id=tg_message_id, chat_id=chat_id, new_text=text)
    finally:
        eng.dispose()


def window(client, token, chat_id, **params):
    return client.get(f"{CHATS}/{chat_id}/window", headers=auth(token), params=params)


def texts(resp) -> list[str]:
    return [m["text"] for m in resp.json()["items"]]


@pytest.fixture
def world(client):
    """Admin + two accounts, each with its own archive.
    Alice: chat 200 with ten messages, chat 201 with two.
    Bob: chat 300 with two."""
    admin = make_admin(client)
    alice, alice_id = make_user(client, admin, "alice")
    bob, bob_id = make_user(client, admin, "bob")
    archives = {"alice": provision(client, alice), "bob": provision(client, bob)}
    seed_archive_by_name(archives["alice"], chat_id=200, name="alice-big", messages=[(i, 11, f"m{i}") for i in range(1, 11)])
    seed_archive_by_name(archives["alice"], chat_id=201, name="alice-small", messages=[(1, 12, "s1"), (2, 12, "s2")])
    seed_archive_by_name(archives["bob"], chat_id=300, name="bob-chat", messages=[(1, 21, "b1"), (2, 21, "b2")])
    return {"admin": admin, "alice": alice, "bob": bob, "archives": archives, "alice_id": alice_id, "bob_id": bob_id}


def message_id(world, who: str, chat_id: int, text: str) -> int:
    found = archive_scalar(world["archives"][who], "SELECT id FROM messages WHERE chat_id = :c AND text = :t", c=chat_id, t=text)
    assert found is not None, f"no message {text!r} in chat {chat_id}"
    return found


# ---------------------------------------------------------------------------
# A1 - edit counts reach the API models
# ---------------------------------------------------------------------------


class TestEditCountsInResponses:
    def test_chat_messages_and_the_chat_itself_report_edits(self, client, world):
        seed_archive_by_name(world["archives"]["alice"], chat_id=202, name="edited", messages=[(1, 11, "v1"), (2, 11, "plain")], edited={1: "v2"})
        archive_edit_twice(world["archives"]["alice"], 202, 1)  # three edits in total

        rows = client.get(f"{CHATS}/202/messages", headers=auth(world["alice"]), params={"order": "asc"}).json()["items"]
        assert [r["edit_count"] for r in rows] == [3, 0]

        chat = client.get(f"{CHATS}/202", headers=auth(world["alice"])).json()
        assert chat["edited_count"] == 1  # messages that were ever edited, not the number of edits

    def test_the_global_feed_and_the_chat_list_have_the_new_fields(self, client, world):
        feed = client.get("/api/messages", headers=auth(world["alice"])).json()["items"]
        assert all(isinstance(m["edit_count"], int) for m in feed)
        chats = client.get(CHATS, headers=auth(world["alice"])).json()["items"]
        assert all(isinstance(c["edited_count"], int) for c in chats)


# ---------------------------------------------------------------------------
# A2 - GET /api/chats/{chat_id}/window
# ---------------------------------------------------------------------------


class TestWindow:
    def test_opens_at_the_newest_messages_oldest_first(self, client, world):
        resp = window(client, world["alice"], 200, limit=4)
        assert resp.status_code == 200, resp.text
        assert texts(resp) == ["m7", "m8", "m9", "m10"]
        body = resp.json()
        assert (body["has_older"], body["has_newer"], body["anchor_id"]) == (True, False, None)

    def test_the_default_limit_covers_a_small_chat(self, client, world):
        resp = window(client, world["alice"], 200)
        assert resp.status_code == 200 and len(resp.json()["items"]) == 10 and resp.json()["has_older"] is False

    def test_rows_have_the_message_shape_without_a_chat_object(self, client, world):
        row = window(client, world["alice"], 201).json()["items"][0]
        assert {"id", "tg_message_id", "text", "date", "is_edited", "edit_count", "is_deleted", "sender"} <= set(row)
        assert row["chat"] is None and row["sender"]["sender_id"] == 12

    def test_around_a_message_returns_its_neighbours_and_names_the_anchor(self, client, world):
        anchor = message_id(world, "alice", 200, "m5")
        resp = window(client, world["alice"], 200, around=anchor, limit=5)
        assert resp.status_code == 200, resp.text
        assert texts(resp) == ["m3", "m4", "m5", "m6", "m7"] and resp.json()["anchor_id"] == anchor

    def test_the_client_can_walk_back_with_before_using_ids_it_already_has(self, client, world):
        first = window(client, world["alice"], 200, limit=4).json()
        older = window(client, world["alice"], 200, limit=4, before=first["items"][0]["id"])
        assert texts(older) == ["m3", "m4", "m5", "m6"] and older.json()["has_older"] is True
        oldest = window(client, world["alice"], 200, limit=4, before=older.json()["items"][0]["id"])
        assert texts(oldest) == ["m1", "m2"] and oldest.json()["has_older"] is False

    def test_after_walks_forward(self, client, world):
        anchor = message_id(world, "alice", 200, "m8")
        resp = window(client, world["alice"], 200, after=anchor, limit=5)
        assert texts(resp) == ["m9", "m10"] and resp.json()["has_newer"] is False

    @pytest.mark.parametrize("moment", ["2026-01-01T12:04:00+00:00", "2026-01-01T12:04:00Z", "2026-01-01T12:04:00"])
    def test_around_a_date_accepts_iso_8601_with_or_without_an_offset(self, client, world, moment):
        """Without an offset the moment is read as UTC (documented on the route); the seeded messages are one minute apart from 12:00 UTC."""
        resp = window(client, world["alice"], 200, around_date=moment, limit=3)
        assert resp.status_code == 200, resp.text
        assert texts(resp) == ["m4", "m5", "m6"]

    def test_a_date_only_value_is_midnight_utc(self, client, world):
        resp = window(client, world["alice"], 200, around_date="2026-01-01", limit=3)
        assert resp.status_code == 200, resp.text
        assert resp.json()["anchor_id"] == message_id(world, "alice", 200, "m1")


class TestWindowInputs:
    @pytest.mark.parametrize("limit", [0, -1, chats_routes.CHAT_WINDOW_MAX + 1, "many"])
    def test_limit_is_bounded(self, client, world, limit):
        assert window(client, world["alice"], 200, limit=limit).status_code == 422

    def test_the_largest_allowed_limit_works(self, client, world):
        assert window(client, world["alice"], 200, limit=chats_routes.CHAT_WINDOW_MAX).status_code == 200

    @pytest.mark.parametrize("param", ["around", "before", "after"])
    @pytest.mark.parametrize("value", ["0", "-5", str(2**63), "abc"])
    def test_cursors_must_be_valid_ids(self, client, world, param, value):
        """Out of the 64-bit range must be a 422 here, not a numeric-overflow error from Postgres (a 500)."""
        assert window(client, world["alice"], 200, **{param: value}).status_code == 422

    @pytest.mark.parametrize("moment", ["not-a-date", "2026-13-45", "x" * 500, "2026-01-01T00:00:00+99:99", "0001-01-01T00:00:00+01:00"])
    def test_a_bad_or_oversized_date_is_a_client_error(self, client, world, moment):
        assert window(client, world["alice"], 200, around_date=moment).status_code == 422

    def test_two_anchors_are_refused_with_a_translatable_reason(self, client, world):
        resp = window(client, world["alice"], 200, around=1, before=2)
        assert resp.status_code == 422
        assert resp.json()["detail"]["reason"] == "too_many_anchors"

    def test_a_chat_id_beyond_64_bits_is_a_client_error(self, client, world):
        assert window(client, world["alice"], 2**63).status_code == 422


class TestWindowAccess:
    def test_an_unknown_chat_is_a_404(self, client, world):
        resp = window(client, world["alice"], 999)
        assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "chat_not_found"

    def test_a_message_of_another_chat_is_not_an_anchor(self, client, world):
        foreign = message_id(world, "alice", 201, "s1")
        for param in ("around", "before", "after"):
            resp = window(client, world["alice"], 200, **{param: foreign})
            assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "message_not_found", param

    def test_one_account_cannot_read_the_others_archive(self, client, world):
        """Bob asks for Alice's chat id: his own archive has no such chat, so it is simply not there. Admin status changes nothing."""
        assert window(client, world["bob"], 200).status_code == 404
        assert window(client, world["admin"], 200).status_code == 409  # the admin has no archive of their own, and does not get Alice's
        assert texts(window(client, world["bob"], 300)) == ["b1", "b2"]

    def test_a_message_id_of_another_account_is_not_an_anchor_either(self, client, world):
        alice_message = message_id(world, "alice", 200, "m5")
        # Bob's chat 300 has two messages with small internal ids; use an id that exists only in Alice's archive.
        resp = window(client, world["bob"], 300, around=alice_message)
        assert resp.status_code == 404

    def test_it_needs_a_login(self, client, world):
        assert client.get(f"{CHATS}/200/window").status_code == 401

    def test_it_cannot_write(self, client, world):
        """The route uses the READ-ONLY archive dependency: reading a conversation must never be able to change it."""
        import inspect

        from api import dependencies

        route = next(r for r in chats_routes.router.routes if getattr(r, "path", "") == "/chats/{chat_id}/window")
        defaults = [p.default for p in inspect.signature(route.endpoint).parameters.values()]  # type: ignore[attr-defined]
        assert any(getattr(d, "dependency", None) is dependencies.get_archive_connection for d in defaults)
        assert not any(getattr(d, "dependency", None) is dependencies.get_archive_write_connection for d in defaults)


# ---------------------------------------------------------------------------
# A3 - DELETE /api/chats/{chat_id}
# ---------------------------------------------------------------------------


def purge(client, token, chat_id):
    return client.delete(f"{CHATS}/{chat_id}", headers=auth(token))


def chat_ids(client, token) -> list[int]:
    return sorted(c["chat_id"] for c in client.get(f"{CHATS}/options", headers=auth(token)).json())


class TestPurgeChat:
    def test_the_chat_and_everything_in_it_disappear_everywhere(self, client, world):
        resp = purge(client, world["alice"], 200)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["chat_id"] == 200 and body["messages_deleted"] == 10
        assert {"edits_deleted", "deletions_deleted", "senders_deleted", "backfill_runs_deleted", "block_rule_kept"} <= set(body)

        # Gone from the chat list, from the pickers' options, from the feed and from its own endpoints.
        assert chat_ids(client, world["alice"]) == [201]
        assert [c["chat_id"] for c in client.get(CHATS, headers=auth(world["alice"])).json()["items"]] == [201]
        feed = client.get("/api/messages", headers=auth(world["alice"]), params={"q": "m5"}).json()
        assert feed["total"] == 0
        assert client.get(f"{CHATS}/200", headers=auth(world["alice"])).status_code == 404
        assert window(client, world["alice"], 200).status_code == 404

    def test_the_other_chats_of_the_same_account_are_untouched(self, client, world):
        purge(client, world["alice"], 200)
        assert texts(window(client, world["alice"], 201)) == ["s1", "s2"]

    def test_edited_and_deleted_messages_leave_no_text_behind(self, client, world):
        archive = world["archives"]["alice"]
        seed_archive_by_name(archive, chat_id=205, name="secret", messages=[(1, 11, "needle original"), (2, 11, "needle gone")], edited={1: "needle edited"}, deleted=[2])
        assert purge(client, world["alice"], 205).status_code == 200
        for table, column in (("messages", "text"), ("message_edits", "old_text"), ("message_edits", "new_text"), ("message_deletions", "text_snapshot")):
            assert archive_scalar(archive, f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE '%needle%'") == 0, (table, column)

    def test_an_unknown_chat_is_a_404_and_purging_twice_is_too(self, client, world):
        resp = purge(client, world["alice"], 999)
        assert resp.status_code == 404 and resp.json()["detail"]["reason"] == "chat_not_found"
        assert purge(client, world["alice"], 201).status_code == 200
        assert purge(client, world["alice"], 201).status_code == 404

    def test_a_block_rule_stays_and_the_response_says_so(self, client, world):
        assert client.put(f"/api/ingest/blocklist/200", headers=auth(world["alice"])).status_code == 200
        body = purge(client, world["alice"], 200).json()
        assert body["block_rule_kept"] is True
        listed = client.get("/api/ingest/blocklist", headers=auth(world["alice"])).json()
        assert [r["chat_id"] for r in listed["items"]] == [200]

    def test_the_chat_comes_back_only_when_the_archiver_writes_to_it_again(self, client, world):
        purge(client, world["alice"], 201)
        assert 201 not in chat_ids(client, world["alice"])
        seed_archive_by_name(world["archives"]["alice"], chat_id=201, name="alice-small", messages=[(9, 12, "hello again")])  # what the live handler would do
        assert texts(window(client, world["alice"], 201)) == ["hello again"]


class TestPurgeAccess:
    def test_another_account_cannot_purge_it_and_nothing_changes(self, client, world):
        """The key isolation test: Bob names Alice's chat id. His archive has no such chat, and Alice's data must be exactly as before."""
        assert purge(client, world["bob"], 200).status_code == 404
        assert len(window(client, world["alice"], 200).json()["items"]) == 10
        assert chat_ids(client, world["alice"]) == [200, 201]
        assert purge(client, world["alice"], 300).status_code == 404  # and the other way round
        assert chat_ids(client, world["bob"]) == [300]

    def test_the_admin_has_no_special_reach(self, client, world):
        assert purge(client, world["admin"], 200).status_code == 409  # no archive of their own (archive_unattached) - and not Alice's
        assert len(window(client, world["alice"], 200).json()["items"]) == 10

    def test_it_needs_a_login(self, client, world):
        assert client.delete(f"{CHATS}/200").status_code == 401

    def test_a_chat_id_beyond_64_bits_is_a_client_error(self, client, world):
        assert purge(client, world["alice"], 2**63).status_code == 422
        assert purge(client, world["alice"], -(2**63) - 1).status_code == 422

    def test_it_uses_the_write_connection_and_only_here(self, client, world):
        """DELETE is the one route in this file that may modify an archive; every GET must stay on the read-only connection."""
        import inspect

        from api import dependencies

        def uses(route, dependency) -> bool:
            defaults = [p.default for p in inspect.signature(route.endpoint).parameters.values()]
            return any(getattr(d, "dependency", None) is dependency for d in defaults)

        writers = [
            (sorted(r.methods), r.path)  # type: ignore[attr-defined]
            for r in chats_routes.router.routes
            if uses(r, dependencies.get_archive_write_connection)
        ]
        assert writers == [(["DELETE"], "/chats/{chat_id}")]


class TestPurgeAndBackfill:
    def test_it_is_refused_while_a_backfill_is_running_and_nothing_is_deleted(self, client, world):
        """A running backfill could re-insert the very history being removed, so the purge waits for it to finish."""
        import json
        import os

        from utils.worker_paths import backfill_status_path

        path = backfill_status_path(world["alice_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))  # this very process is a live pid
        try:
            resp = purge(client, world["alice"], 200)
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "backfill_running"
            assert len(window(client, world["alice"], 200).json()["items"]) == 10
        finally:
            path.unlink()
        assert purge(client, world["alice"], 200).status_code == 200  # the same request goes through once the backfill is over

    def test_a_stale_status_file_does_not_block_it(self, client, world):
        """A crashed backfill leaves 'running' behind; its pid is dead, so that must not lock the owner out of purging."""
        import json

        from utils.worker_paths import backfill_status_path

        path = backfill_status_path(world["alice_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "running", "pid": 2**22 + 12345}))
        try:
            assert purge(client, world["alice"], 200).status_code == 200
        finally:
            path.unlink()

    def test_another_accounts_backfill_does_not_block_this_one(self, client, world):
        import json
        import os

        from utils.worker_paths import backfill_status_path

        path = backfill_status_path(world["bob_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        try:
            assert purge(client, world["alice"], 200).status_code == 200
        finally:
            path.unlink()