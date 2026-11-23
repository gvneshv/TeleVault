"""
POST /api/backfill/start and the backfill cooldown, and POST /api/media/retry.

Starting: the cooldown answers 429 with the seconds left BEFORE a process is spawned; only a completed run starts it; the `full` flag reaches backfill.py.
Retrying files: only the caller's own archive is touched, only retryable statuses are accepted, nothing is downloaded.
No real process is ever started: subprocess.Popen is replaced.
"""

import dataclasses
from types import SimpleNamespace
from unittest import mock

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
from api.routes import backfill as backfill_routes
from tests.helpers import T0, auth, make_admin, make_user

START = "/api/backfill/start"
RETRY = "/api/media/retry"
DAY = 24 * 3600


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def on_archive(name, fn):
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            result = fn(conn)
            conn.commit()
            return result
    finally:
        eng.dispose()


@pytest.fixture
def world(client, control_engine):
    admin = make_admin(client)
    users = {}
    for name in ("alice", "bob"):
        token, user_id = make_user(client, admin, name)
        with control_engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE users SET telegram_api_id = 'x', telegram_api_hash = 'x', telegram_session_string = 'x' WHERE id = :i"),
                {"i": user_id},
            )
        users[name] = {"token": token, "archive": provision(client, token)}
    return users


@pytest.fixture
def spawned(monkeypatch):
    """Replace subprocess.Popen in the route module; yields the list of commands that would have been started."""
    commands: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(backfill_routes.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(backfill_routes, "_running_pid", lambda user_id: None)
    monkeypatch.setattr(backfill_routes, "_telethon_is_running", lambda user_id: False)
    return commands


@pytest.fixture
def cooldown_on(monkeypatch):
    monkeypatch.setattr(backfill_routes, "settings", dataclasses.replace(backfill_routes.settings, backfill_cooldown_seconds=DAY))


def add_run(name, status, finished_ago):
    on_archive(
        name,
        lambda c: c.execute(
            sa.text("INSERT INTO backfill_runs (status, finished_at) VALUES (:s, now() - make_interval(secs => :a))"),
            {"s": status, "a": finished_ago},
        ),
    )


class TestStart:
    def test_a_first_backfill_starts(self, client, world, spawned, cooldown_on):
        resp = client.post(START, json={}, headers=auth(world["alice"]["token"]))
        assert resp.status_code == 200, resp.text
        assert len(spawned) == 1 and "--full" not in spawned[0]

    def test_within_the_period_after_a_completed_run_it_is_refused_and_nothing_starts(self, client, world, spawned, cooldown_on):
        add_run(world["alice"]["archive"], "completed", 3600)
        resp = client.post(START, json={}, headers=auth(world["alice"]["token"]))
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert detail["reason"] == "backfill_cooldown"
        assert DAY - 3600 - 10 <= detail["retry_after_seconds"] <= DAY - 3600
        assert resp.headers["Retry-After"] == str(detail["retry_after_seconds"])
        assert spawned == []

    def test_a_single_chat_is_held_to_the_same_rule(self, client, world, spawned, cooldown_on):
        add_run(world["alice"]["archive"], "completed", 60)
        assert client.post(START, json={"chat": "12"}, headers=auth(world["alice"]["token"])).status_code == 429

    def test_after_the_period_it_starts_again(self, client, world, spawned, cooldown_on):
        add_run(world["alice"]["archive"], "completed", DAY + 5)
        assert client.post(START, json={}, headers=auth(world["alice"]["token"])).status_code == 200

    @pytest.mark.parametrize("status", ["cancelled", "error"])
    def test_a_run_that_did_not_complete_does_not_hold_it_back(self, client, world, spawned, cooldown_on, status):
        add_run(world["alice"]["archive"], status, 5)
        assert client.post(START, json={}, headers=auth(world["alice"]["token"])).status_code == 200

    def test_the_cooldown_is_per_account(self, client, world, spawned, cooldown_on):
        add_run(world["alice"]["archive"], "completed", 60)
        assert client.post(START, json={}, headers=auth(world["bob"]["token"])).status_code == 200

    def test_with_the_cooldown_off_it_never_refuses(self, client, world, spawned):
        add_run(world["alice"]["archive"], "completed", 1)  # the test environment has BACKFILL_COOLDOWN_HOURS=0
        assert client.post(START, json={}, headers=auth(world["alice"]["token"])).status_code == 200

    def test_full_reaches_the_worker(self, client, world, spawned, cooldown_on):
        assert client.post(START, json={"full": True}, headers=auth(world["alice"]["token"])).status_code == 200
        assert "--full" in spawned[0]


def seed_media(name, statuses):
    def seed(conn):
        import db.queries as q

        q.upsert_chat(conn, chat_id=11, name="c", chat_type="private")
        for i, status in enumerate(statuses, start=1):
            q.insert_message(conn, tg_message_id=i, chat_id=11, sender_id=None, text=None, date=T0, media_kind="video", media_size=10)
            conn.execute(
                sa.text("UPDATE message_media SET status = :s, file_path = :p WHERE message_id = (SELECT id FROM messages WHERE tg_message_id = :t)"),
                {"s": status, "p": "users/1/media/1" if status == "stored" else None, "t": i},
            )
        conn.commit()

    on_archive(name, seed)


def statuses(name):
    return on_archive(name, lambda c: c.execute(sa.text("SELECT status FROM message_media ORDER BY id")).scalars().all())


class TestRetry:
    def test_requeues_the_refused_and_failed_files_of_the_caller_only(self, client, world):
        seed_media(world["alice"]["archive"], ["too_large", "disk_full", "failed", "unavailable", "stored"])
        seed_media(world["bob"]["archive"], ["too_large"])
        resp = client.post(RETRY, json={}, headers=auth(world["alice"]["token"]))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"requeued": 3}
        assert statuses(world["alice"]["archive"]) == ["pending", "pending", "pending", "unavailable", "stored"]
        assert statuses(world["bob"]["archive"]) == ["too_large"]

    def test_can_be_limited_to_some_statuses(self, client, world):
        seed_media(world["alice"]["archive"], ["too_large", "disk_full"])
        resp = client.post(RETRY, json={"statuses": ["disk_full"]}, headers=auth(world["alice"]["token"]))
        assert resp.json() == {"requeued": 1}
        assert statuses(world["alice"]["archive"]) == ["too_large", "pending"]

    @pytest.mark.parametrize("bad", [["stored"], ["unavailable"], ["pending"], ["nope"], ["failed"] * 4, "failed"])
    def test_a_status_that_must_not_be_retried_is_refused_at_the_edge(self, client, world, bad):
        seed_media(world["alice"]["archive"], ["stored"])
        assert client.post(RETRY, json={"statuses": bad}, headers=auth(world["alice"]["token"])).status_code == 422
        assert statuses(world["alice"]["archive"]) == ["stored"]

    def test_nothing_to_retry_is_zero_not_an_error(self, client, world):
        assert client.post(RETRY, json={}, headers=auth(world["alice"]["token"])).json() == {"requeued": 0}

    def test_a_signed_out_caller_is_refused(self, client):
        assert client.post(RETRY, json={}).status_code == 401
