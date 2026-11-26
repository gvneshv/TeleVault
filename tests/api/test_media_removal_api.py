"""Deleting a chat or all stored data also deletes the downloaded files of those messages - and only those, and never from another account's folder."""

import shutil

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
import db.queries as q
from tests.api.test_chat_view_api import purge, world  # noqa: F401  (shared fixture and helper)
from tests.helpers import T0, auth
from utils.worker_paths import media_dir, media_relative_path


def store(archive_name: str, user_id: int, chat_id: int, tg_id: int):
    """Give a message of this chat a stored photo: the row says so and the file exists.
    Returns the file."""
    eng = sa.create_engine(db.server_database_url(archive_name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            q.insert_message(conn, tg_message_id=tg_id, chat_id=chat_id, sender_id=None, text=None, date=T0, media_kind="photo", media_size=3)
            media_id = int(conn.execute(
                sa.text("SELECT mm.id FROM message_media mm JOIN messages m ON m.id = mm.message_id WHERE m.chat_id = :c AND m.tg_message_id = :t"),
                {"c": chat_id, "t": tg_id},
            ).scalar_one())
            conn.execute(
                sa.text("UPDATE message_media SET status = 'stored', file_path = :p WHERE id = :i"),
                {"p": media_relative_path(user_id, media_id), "i": media_id},
            )
            conn.commit()
    finally:
        eng.dispose()
    file = media_dir(user_id) / str(media_id)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"abc")
    return file


@pytest.fixture(autouse=True)
def clean_folders(world):  # noqa: F811
    for key in ("alice_id", "bob_id"):
        shutil.rmtree(media_dir(world[key]), ignore_errors=True)
    yield


class TestDeleteChat:
    def test_the_chats_files_are_deleted_and_other_chats_files_stay(self, client, world):  # noqa: F811
        gone = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        stays = store(world["archives"]["alice"], world["alice_id"], 201, 91)
        resp = purge(client, world["alice"], 200)
        assert resp.status_code == 200, resp.text
        assert not gone.exists()
        assert stays.exists()

    def test_the_stored_paths_never_reach_the_client(self, client, world):  # noqa: F811
        store(world["archives"]["alice"], world["alice_id"], 200, 90)
        assert "media_paths" not in purge(client, world["alice"], 200).json()

    def test_a_refused_purge_deletes_no_file(self, client, world):  # noqa: F811
        """Bob names Alice's chat id: 404, and Alice's file is untouched."""
        mine = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        assert purge(client, world["bob"], 200).status_code == 404
        assert mine.exists()


class TestDeleteAllData:
    URL = "/api/archive/data"

    def test_every_file_of_the_account_goes_and_another_accounts_stay(self, client, world):  # noqa: F811
        a1 = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        a2 = store(world["archives"]["alice"], world["alice_id"], 201, 91)
        bobs = store(world["archives"]["bob"], world["bob_id"], 300, 90)
        resp = client.delete(self.URL, headers=auth(world["alice"]), params={"confirm": "delete-all-data"})
        assert resp.status_code == 200, resp.text
        assert "media_paths" not in resp.json()
        assert not a1.exists() and not a2.exists()
        assert bobs.exists()

    def test_a_wrong_confirmation_deletes_nothing(self, client, world):  # noqa: F811
        mine = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        assert client.delete(self.URL, headers=auth(world["alice"]), params={"confirm": "nope"}).status_code == 422
        assert mine.exists()