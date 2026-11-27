"""
GET /api/media/file/{message_id} (serving a stored file) and GET /api/media/usage (what the files take), plus the media fields on message lists.

The file route is the one place archived, untrusted content is sent back to a browser, so most tests here are about what must NOT happen:
another account's file, a file that is not stored, a path that was tampered with, and a hostile file being shown inline.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
from tests.api.test_chat_view_api import archive_scalar, message_id, window, world  # noqa: F401  (shared fixture and helpers)
from tests.api.test_media_removal_api import clean_folders, store  # noqa: F401
from tests.helpers import auth
from utils.worker_paths import media_dir

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20
HTML = b"<html><script>alert(1)</script></html>"
FILE = "/api/media/file"
USAGE = "/api/media/usage"


def put_content(file, data: bytes):
    file.write_bytes(data)


def message_of(world, who, chat_id, tg_id) -> int:  # noqa: F811
    found = archive_scalar(world["archives"][who], "SELECT id FROM messages WHERE chat_id = :c AND tg_message_id = :t", c=chat_id, t=tg_id)
    assert found is not None
    return int(found)


class TestServing:
    def test_a_stored_photo_is_sent_inline_with_protective_headers(self, client, world):  # noqa: F811
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, JPEG)
        resp = client.get(f"{FILE}/{message_of(world, 'alice', 200, 90)}", headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        assert resp.content == JPEG
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["content-disposition"].startswith("inline;")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in resp.headers["content-security-policy"]
        assert "private" in resp.headers["cache-control"]

    def test_a_hostile_file_is_never_shown_inline_whatever_the_message_claims(self, client, world):  # noqa: F811
        """Stored as a 'photo', but the bytes are HTML: it must be a download, not a page in the app's origin."""
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, HTML)
        resp = client.get(f"{FILE}/{message_of(world, 'alice', 200, 90)}", headers=auth(world["alice"]))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["content-disposition"].startswith("attachment;")
        assert resp.headers["content-disposition"].endswith('.bin"')

    def test_a_message_without_a_stored_file_has_nothing_to_send(self, client, world):  # noqa: F811
        plain = message_id(world, "alice", 200, "m1")
        resp = client.get(f"{FILE}/{plain}", headers=auth(world["alice"]))
        assert resp.status_code == 404
        assert resp.json()["detail"]["reason"] == "media_not_stored"

    def test_a_file_that_is_not_stored_yet_is_not_served(self, client, world):  # noqa: F811
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, JPEG)
        mid = message_of(world, "alice", 200, 90)
        archive_scalar(world["archives"]["alice"], "UPDATE message_media SET status = 'pending', file_path = NULL WHERE message_id = :m", m=mid)
        assert client.get(f"{FILE}/{mid}", headers=auth(world["alice"])).status_code == 404

    def test_a_record_whose_file_vanished_is_a_clean_404(self, client, world):  # noqa: F811
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        file.unlink()
        assert client.get(f"{FILE}/{message_of(world, 'alice', 200, 90)}", headers=auth(world["alice"])).status_code == 404

    def test_another_account_cannot_fetch_it(self, client, world):  # noqa: F811
        """Bob asks for Alice's message id. His archive has no such row; Alice's file is not reachable through his account."""
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, JPEG)
        mid = message_of(world, "alice", 200, 90)
        assert client.get(f"{FILE}/{mid}", headers=auth(world["bob"])).status_code == 404

    def test_a_tampered_path_pointing_at_another_accounts_folder_is_refused(self, client, world):  # noqa: F811
        """Even if a row said `users/<bob>/media/..`, Alice's request must not serve Bob's file."""
        bobs = store(world["archives"]["bob"], world["bob_id"], 300, 90)
        put_content(bobs, JPEG)
        mine = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        mid = message_of(world, "alice", 200, 90)
        archive_scalar(
            world["archives"]["alice"], "UPDATE message_media SET file_path = :p WHERE message_id = :m", p=f"users/{world['bob_id']}/media/{bobs.name}", m=mid
        )
        assert mine.exists()
        assert client.get(f"{FILE}/{mid}", headers=auth(world["alice"])).status_code == 404

    @pytest.mark.parametrize("bad", ["../../../etc/passwd", "users/1/media/../../x", "/etc/passwd"])
    def test_a_path_that_is_not_the_downloaders_shape_is_refused(self, client, world, bad):  # noqa: F811
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, JPEG)
        mid = message_of(world, "alice", 200, 90)
        archive_scalar(world["archives"]["alice"], "UPDATE message_media SET file_path = :p WHERE message_id = :m", p=bad, m=mid)
        assert client.get(f"{FILE}/{mid}", headers=auth(world["alice"])).status_code == 404

    def test_it_needs_a_login_and_a_sane_id(self, client, world):  # noqa: F811
        assert client.get(f"{FILE}/1").status_code == 401
        assert client.get(f"{FILE}/{2**63}", headers=auth(world["alice"])).status_code == 422
        assert client.get(f"{FILE}/0", headers=auth(world["alice"])).status_code == 422


class TestUsage:
    def test_counts_bytes_and_the_limits(self, client, world):  # noqa: F811
        file = store(world["archives"]["alice"], world["alice_id"], 200, 90)
        put_content(file, JPEG)
        archive_scalar(world["archives"]["alice"], "UPDATE message_media SET size_bytes = 1000")
        store(world["archives"]["alice"], world["alice_id"], 200, 91)  # a second stored file
        resp = client.get(USAGE, headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored_files"] == 2
        assert body["stored_bytes"] >= 1000
        assert body["counts"]["stored"] == 2
        assert set(body["counts"]) == {"pending", "stored", "too_large", "disk_full", "unavailable", "failed", "skipped"}
        assert body["limit_bytes"] > 0 and body["max_file_bytes"] > 0

    def test_each_account_sees_only_its_own_files(self, client, world):  # noqa: F811
        store(world["archives"]["alice"], world["alice_id"], 200, 90)
        assert client.get(USAGE, headers=auth(world["bob"])).json()["stored_files"] == 0

    def test_an_empty_archive_is_all_zeros(self, client, world):  # noqa: F811
        body = client.get(USAGE, headers=auth(world["bob"])).json()
        assert body["stored_bytes"] == 0 and set(body["counts"].values()) == {0}

    def test_it_needs_a_login(self, client):
        assert client.get(USAGE).status_code == 401


class TestMessagesCarryTheFileState:
    def test_a_message_list_says_whether_the_file_is_stored_and_how_big(self, client, world):  # noqa: F811
        store(world["archives"]["alice"], world["alice_id"], 200, 90)
        archive_scalar(world["archives"]["alice"], "UPDATE message_media SET size_bytes = 777")
        item = [m for m in window(client, world["alice"], 200).json()["items"] if m["media_kind"]][0]
        assert (item["media_kind"], item["media_status"], item["media_size"]) == ("photo", "stored", 777)

    def test_plain_messages_have_none_of_it(self, client, world):  # noqa: F811
        plain = window(client, world["alice"], 201).json()["items"][0]
        assert (plain["media_kind"], plain["media_status"], plain["media_size"]) == (None, None, None)