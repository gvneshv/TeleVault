"""Accounts must never see each other's archives - including the admin's view of other people's messages."""

import pytest

from tests.helpers import auth, make_admin, make_user, seed_archive_by_name


def _archive_name(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


@pytest.fixture
def world(client):
    """Admin + two users, each with their own provisioned archive holding distinctive messages."""
    admin = make_admin(client)
    alice_token, alice_id = make_user(client, admin, "alice")
    bob_token, bob_id = make_user(client, admin, "bob")
    archives = {}
    for token, who in ((admin, "admin"), (alice_token, "alice"), (bob_token, "bob")):
        name = archives[who] = _archive_name(client, token)
        seed_archive_by_name(
            name, chat_id={"admin": 100, "alice": 200, "bob": 300}[who], name=f"{who}-chat",
            messages=[(1, 1, f"{who} secret one"), (2, 1, f"{who} secret two"), (3, 1, f"{who} only") if who == "bob" else (3, 1, f"{who} third")],
            deleted=[2],
        )
    return {"admin": admin, "alice": alice_token, "bob": bob_token, "alice_id": alice_id, "bob_id": bob_id, "archives": archives}


def texts(resp):
    return {m["text"] for m in resp.json()["items"]}


def test_each_account_reads_only_its_own_messages(client, world):
    for who in ("admin", "alice", "bob"):
        got = texts(client.get("/api/messages", headers=auth(world[who])))
        assert got and all(t.startswith(who) for t in got), (who, got)


def test_the_admin_has_no_special_access_to_other_peoples_messages(client, world):
    got = texts(client.get("/api/messages?per_page=200", headers=auth(world["admin"])))
    assert not any(t.startswith(("alice", "bob")) for t in got)


def test_search_cannot_reach_across_accounts(client, world):
    for token in (world["alice"], world["admin"]):
        assert client.get("/api/messages?q=bob", headers=auth(token)).json()["total"] == 0


def test_chat_listing_and_filters_are_per_account(client, world):
    assert {c["name"] for c in client.get("/api/chats", headers=auth(world["alice"])).json()["items"]} == {"alice-chat"}
    assert client.get("/api/chats/300", headers=auth(world["alice"])).status_code == 404  # bob's chat id
    assert client.get("/api/chats/300/messages", headers=auth(world["alice"])).status_code == 404
    assert client.get("/api/messages?chat_ids=300", headers=auth(world["alice"])).json()["total"] == 0


def test_deleted_view_and_stats_are_per_account(client, world):
    assert texts(client.get("/api/deleted", headers=auth(world["alice"]))) == {"alice secret two"}
    stats = client.get("/api/stats", headers=auth(world["bob"])).json()
    assert stats["total_messages"] == 3 and [c["name"] for c in stats["per_chat"]] == ["bob-chat"]


def test_message_ids_do_not_leak_across_accounts(client, world):
    """Internal ids restart in every archive. An id that exists only in another account's archive must be a plain 404."""
    # Give bob extra messages so he owns ids that alice's archive (3 messages) does not have.
    seed_archive_by_name(world["archives"]["bob"], chat_id=301, name="bob-extra", messages=[(i, 1, f"bob extra {i}") for i in range(1, 8)])
    resp = client.get("/api/messages/9", headers=auth(world["alice"]))
    assert resp.status_code == 404
    assert client.get("/api/messages/9", headers=auth(world["bob"])).status_code == 200


def test_the_api_cannot_write_to_an_archive(client, world):
    """Defence in depth: even a handler bug could not modify archived data, because every API connection is read-only."""
    import sqlalchemy as sa

    import db

    with db.get_tenant_readonly_connection(world["archives"]["alice"]) as ro:
        with pytest.raises(Exception, match="read-only"):
            ro.execute(sa.text("DELETE FROM messages"))
