"""
GET /api/backfill/chats: the chat picker of the backfill dialog.

The list itself comes from the file the account's archiver publishes (dialog_catalog.py); the API adds what changes independently of it:
the blocklist (blocked chats are left out) and the owner's own labels (shown for private chats).
Never another account's chats. No Telegram is involved: the snapshot file is written directly.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import dialog_catalog
from tests.helpers import auth, make_admin, make_user
from utils.worker_paths import dialogs_path

URL = "/api/backfill/chats"

CATALOG = [
    {"chat_id": 7, "name": "Alice Smith", "chat_type": "private"},
    {"chat_id": 8, "name": "bob", "chat_type": "private"},
    {"chat_id": -5, "name": "Family", "chat_type": "group"},
    {"chat_id": -1001, "name": "Dev chat", "chat_type": "supergroup"},
    {"chat_id": -1002, "name": "News", "chat_type": "channel"},
    {"chat_id": 9, "name": None, "chat_type": "private"},
]


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def on_archive(name, sql, **params):
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text(sql), params)
    finally:
        eng.dispose()


def label(archive, sender_id, text):
    """The owner's own label for a person (a label can only exist for a sender the archive knows, so the sender is created first)."""
    on_archive(archive, "INSERT INTO senders (sender_id, first_name) VALUES (:i, 'x') ON CONFLICT DO NOTHING", i=sender_id)
    on_archive(archive, "INSERT INTO sender_display_names (sender_id, display_name) VALUES (:i, :t)", i=sender_id, t=text)


@pytest.fixture
def world(client):
    admin = make_admin(client)
    users = {}
    for name in ("alice", "bob"):
        token, user_id = make_user(client, admin, name)
        users[name] = {"token": token, "id": user_id, "archive": provision(client, token)}
    yield users
    for who in users.values():
        dialogs_path(who["id"]).unlink(missing_ok=True)


def publish(user, chats=CATALOG):
    dialog_catalog.write_catalog(user["id"], chats)


def get(client, user):
    resp = client.get(URL, headers=auth(user["token"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def names(body):
    return [c["name"] for c in body["chats"]]


class TestTheList:
    def test_no_published_list_means_not_available_and_empty(self, client, world):
        assert get(client, world["alice"]) == {"available": False, "updated_at": None, "chats": []}

    def test_a_published_list_is_offered_sorted_by_name_unnamed_last(self, client, world):
        publish(world["alice"])
        body = get(client, world["alice"])
        assert body["available"] is True and body["updated_at"]
        assert names(body) == ["Alice Smith", "bob", "Dev chat", "Family", "News", None]
        assert {c["chat_id"]: c["chat_type"] for c in body["chats"]}[-1001] == "supergroup"

    def test_a_blocked_chat_is_left_out(self, client, world):
        publish(world["alice"])
        on_archive(world["alice"]["archive"], "INSERT INTO ingest_blocked_chats (chat_id, name) VALUES (-5, 'Family'), (8, 'bob')")
        assert names(get(client, world["alice"])) == ["Alice Smith", "Dev chat", "News", None]

    def test_unblocking_brings_it_back_without_a_new_snapshot(self, client, world):
        publish(world["alice"])
        on_archive(world["alice"]["archive"], "INSERT INTO ingest_blocked_chats (chat_id, name) VALUES (-5, 'Family')")
        assert "Family" not in names(get(client, world["alice"]))
        on_archive(world["alice"]["archive"], "DELETE FROM ingest_blocked_chats")
        assert "Family" in names(get(client, world["alice"]))

    def test_the_owners_own_label_replaces_telegrams_name_for_a_private_chat(self, client, world):
        publish(world["alice"])
        label(world["alice"]["archive"], 7, "Mum")
        body = get(client, world["alice"])
        assert names(body) == ["bob", "Dev chat", "Family", "Mum", "News", None]
        assert [c["chat_id"] for c in body["chats"] if c["name"] == "Mum"] == [7]

    def test_a_label_never_renames_a_group_that_happens_to_share_the_number(self, client, world):
        publish(world["alice"], [{"chat_id": -5, "name": "Family", "chat_type": "group"}])
        label(world["alice"]["archive"], -5, "Wrong")
        assert names(get(client, world["alice"])) == ["Family"]

    def test_a_label_gives_a_name_to_a_chat_telegram_gave_none(self, client, world):
        publish(world["alice"])
        label(world["alice"]["archive"], 9, "Neighbour")
        assert "Neighbour" in names(get(client, world["alice"])) and None not in names(get(client, world["alice"]))

    def test_accounts_see_only_their_own_list_blocks_and_labels(self, client, world):
        publish(world["alice"], [{"chat_id": 1, "name": "Only Alice", "chat_type": "private"}])
        publish(world["bob"], [{"chat_id": 2, "name": "Only Bob", "chat_type": "private"}])
        on_archive(world["bob"]["archive"], "INSERT INTO ingest_blocked_chats (chat_id, name) VALUES (1, 'x')")
        label(world["bob"]["archive"], 1, "Bob label")
        assert names(get(client, world["alice"])) == ["Only Alice"]
        assert names(get(client, world["bob"])) == ["Only Bob"]

    def test_a_signed_out_caller_is_refused(self, client):
        assert client.get(URL).status_code == 401

    def test_an_archive_that_predates_the_blocklist_still_answers(self, client, world):
        publish(world["alice"])
        on_archive(world["alice"]["archive"], "DROP TABLE ingest_blocked_chats")
        assert len(get(client, world["alice"])["chats"]) == len(CATALOG)
