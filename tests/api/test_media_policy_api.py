"""
GET/PUT /api/media/policy (which file types are stored) and the backfill `mode` reaching backfill.py.

Per account, validated at the edge, and an archive that was never migrated gets an explained 409 instead of a crash. No real process is started.
"""

import pytest
import sqlalchemy as sa

from tests.api.test_backfill_cooldown_api import START, on_archive, spawned, statuses, world  # noqa: F401  (shared fixtures and helpers)
from tests.helpers import auth

POLICY = "/api/media/policy"
ALL_ON = {"photo": True, "video": True, "voice": True, "audio": True, "document": True}


def seed(name, kinds):
    import db.queries as q
    from tests.helpers import T0

    def go(conn):
        q.upsert_chat(conn, chat_id=11, name="c", chat_type="private")
        for i, kind in enumerate(kinds, start=1):
            q.insert_message(conn, tg_message_id=i, chat_id=11, sender_id=None, text=None, date=T0, media_kind=kind, media_size=10)

    on_archive(name, go)


class TestPolicy:
    def test_everything_is_on_until_changed(self, client, world):  # noqa: F811
        resp = client.get(POLICY, headers=auth(world["alice"]["token"]))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {**ALL_ON, "skipped": 0, "queued": 0}

    def test_saving_is_read_back_and_reports_what_it_did_to_waiting_files(self, client, world):  # noqa: F811
        seed(world["alice"]["archive"], ["photo", "video", "photo"])
        resp = client.put(POLICY, json={**ALL_ON, "photo": False}, headers=auth(world["alice"]["token"]))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {**ALL_ON, "photo": False, "skipped": 2, "queued": 0}
        assert client.get(POLICY, headers=auth(world["alice"]["token"])).json()["photo"] is False
        assert statuses(world["alice"]["archive"]) == ["skipped", "pending", "skipped"]
        back = client.put(POLICY, json=ALL_ON, headers=auth(world["alice"]["token"])).json()
        assert (back["skipped"], back["queued"]) == (0, 2)

    def test_one_account_cannot_change_anothers_choice(self, client, world):  # noqa: F811
        client.put(POLICY, json={k: False for k in ALL_ON}, headers=auth(world["alice"]["token"]))
        assert client.get(POLICY, headers=auth(world["bob"]["token"])).json()["photo"] is True

    @pytest.mark.parametrize("bad", [{}, {"photo": True}, {**ALL_ON, "photo": "maybe"}])
    def test_a_malformed_body_is_refused(self, client, world, bad):  # noqa: F811
        assert client.put(POLICY, json=bad, headers=auth(world["alice"]["token"])).status_code == 422

    def test_signed_out_callers_are_refused(self, client):
        assert client.get(POLICY).status_code == 401
        assert client.put(POLICY, json=ALL_ON).status_code == 401

    def test_an_archive_that_was_not_migrated_gets_an_explained_409(self, client, world):  # noqa: F811
        on_archive(world["alice"]["archive"], lambda c: c.execute(sa.text("DROP TABLE media_skipped_kinds")))
        for resp in (
            client.get(POLICY, headers=auth(world["alice"]["token"])),
            client.put(POLICY, json=ALL_ON, headers=auth(world["alice"]["token"])),
        ):
            assert resp.status_code == 409
            assert resp.json()["detail"]["reason"] == "archive_outdated"


class TestModeReachesTheWorker:
    @pytest.mark.parametrize("mode", ["text", "media"])
    def test_a_mode_becomes_the_flag(self, client, world, spawned, mode):  # noqa: F811
        assert client.post(START, json={"mode": mode}, headers=auth(world["alice"]["token"])).status_code == 200
        assert spawned[0][spawned[0].index("--mode") + 1] == mode

    def test_the_default_adds_no_flag(self, client, world, spawned):  # noqa: F811
        assert client.post(START, json={}, headers=auth(world["alice"]["token"])).status_code == 200
        assert "--mode" not in spawned[0]

    @pytest.mark.parametrize("bad", ["everything", "", "ALL", None])
    def test_an_unknown_mode_is_refused_and_nothing_starts(self, client, world, spawned, bad):  # noqa: F811
        assert client.post(START, json={"mode": bad}, headers=auth(world["alice"]["token"])).status_code == 422
        assert spawned == []
