"""
GET/PUT /api/archive/retention and POST /api/archive/retention/preview (api/routes/archive.py): the owner's "delete messages older than N months" policy.

What these tests pin: the preview deletes nothing;
switching the policy ON needs the number the preview showed and the server re-counts (a stale number is refused with the new count);
switching OFF needs nothing;
limits are enforced at the edge; an archive that has not been migrated answers 409 `archive_outdated`;
the routes use the right connection (reads read-only, the save the writer); accounts are isolated.
"""

import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
from api import dependencies
from api.routes import archive as archive_routes
from tests.helpers import auth, make_admin, make_user, seed_archive_by_name

URL = "/api/archive/retention"
PREVIEW = "/api/archive/retention/preview"


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def on_archive(name, fn):
    eng = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            return fn(conn)
    finally:
        eng.dispose()


def seed_aged(name, ages):
    """One chat with a message per entry of `ages` (days old)."""
    seed_archive_by_name(name, chat_id=42, name="Bob", messages=[(i + 1, 50, f"t{i}") for i in range(len(ages))])

    def age(conn):
        for i, days in enumerate(ages):
            conn.execute(sa.text("UPDATE messages SET date = now() - make_interval(days => :d) WHERE tg_message_id = :m"), {"d": days, "m": i + 1})
        conn.commit()

    on_archive(name, age)


def message_count(name):
    return on_archive(name, lambda c: c.execute(sa.text("SELECT COUNT(*) FROM messages")).scalar())


@pytest.fixture
def world(client):
    admin = make_admin(client)
    alice, _ = make_user(client, admin, "alice")
    bob, _ = make_user(client, admin, "bob")
    return {"alice": alice, "bob": bob, "archives": {"alice": provision(client, alice), "bob": provision(client, bob)}}


def body(**kw):
    return {"enabled": True, "months": 6, "keep_deleted": True, "confirmed_messages": 2, **kw}


class TestRead:
    def test_nothing_saved_is_reported_as_not_configured_and_off(self, client, world):
        resp = client.get(URL, headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert (data["configured"], data["enabled"], data["months"], data["keep_deleted"]) == (False, False, None, True)
        assert (data["min_months"], data["max_months"]) == (1, 120)

    def test_a_signed_out_caller_is_refused(self, client):
        for call in (lambda: client.get(URL), lambda: client.put(URL, json=body()), lambda: client.post(PREVIEW, json={"months": 6})):
            assert call().status_code == 401


class TestPreview:
    def test_it_counts_and_deletes_nothing(self, client, world):
        name = world["archives"]["alice"]
        seed_aged(name, [400, 200, 100, 40, 1])
        resp = client.post(PREVIEW, json={"months": 6, "keep_deleted": True}, headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert (data["count"], data["total"]) == (2, 5)
        assert data["oldest"] and data["newest"] and data["cutoff"]
        assert message_count(name) == 5

    def test_nothing_to_delete_has_no_dates(self, client, world):
        data = client.post(PREVIEW, json={"months": 120}, headers=auth(world["alice"])).json()
        assert (data["count"], data["oldest"], data["newest"]) == (0, None, None)

    @pytest.mark.parametrize("months", [0, 121, -3, "six", None, 1.5])
    def test_months_outside_the_limits_are_refused(self, client, world, months):
        assert client.post(PREVIEW, json={"months": months}, headers=auth(world["alice"])).status_code == 422


class TestSave:
    def test_enabling_with_the_previewed_count_saves(self, client, world):
        seed_aged(world["archives"]["alice"], [400, 200, 100, 40, 1])
        resp = client.put(URL, json=body(), headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert (data["configured"], data["enabled"], data["months"], data["confirmed_messages"]) == (True, True, 6, 2)
        assert client.get(URL, headers=auth(world["alice"])).json() == data

    def test_saving_deletes_nothing_itself(self, client, world):
        name = world["archives"]["alice"]
        seed_aged(name, [400, 200, 100, 40, 1])
        client.put(URL, json=body(), headers=auth(world["alice"]))
        assert message_count(name) == 5  # the job in the archiver does that, later

    def test_enabling_without_a_confirmed_count_is_refused(self, client, world):
        resp = client.put(URL, json=body(confirmed_messages=None), headers=auth(world["alice"]))
        assert resp.status_code == 422 and resp.json()["detail"]["reason"] == "confirmation_required"
        assert client.get(URL, headers=auth(world["alice"])).json()["configured"] is False

    def test_a_stale_confirmation_is_refused_with_the_new_count(self, client, world, monkeypatch):
        seed_aged(world["archives"]["alice"], [400, 200, 100, 40, 1])
        monkeypatch.setattr("utils.retention_rules.RETENTION_CONFIRM_TOLERANCE_MIN", 0)
        monkeypatch.setattr("utils.retention_rules.RETENTION_CONFIRM_TOLERANCE_FRACTION", 0.0)
        resp = client.put(URL, json=body(confirmed_messages=1), headers=auth(world["alice"]))  # 2 would go
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert (detail["reason"], detail["actual"]) == ("preview_outdated", 2)
        assert client.get(URL, headers=auth(world["alice"])).json()["configured"] is False

    def test_a_confirmation_higher_than_the_real_count_is_fine(self, client, world):
        assert client.put(URL, json=body(confirmed_messages=50), headers=auth(world["alice"])).status_code == 200

    def test_switching_off_needs_no_confirmation_and_keeps_the_number(self, client, world):
        client.put(URL, json=body(), headers=auth(world["alice"]))
        resp = client.put(URL, json=body(enabled=False, confirmed_messages=None), headers=auth(world["alice"]))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert (data["enabled"], data["months"], data["confirmed_messages"]) == (False, 6, None)

    def test_a_confirmation_sent_with_enabled_false_is_not_stored(self, client, world):
        """Nothing was confirmed when the policy is off; a leftover number must not become a limit for a later run."""
        data = client.put(URL, json=body(enabled=False, confirmed_messages=5), headers=auth(world["alice"])).json()
        assert data["confirmed_messages"] is None

    def test_the_policy_can_be_replaced(self, client, world):
        client.put(URL, json=body(months=6), headers=auth(world["alice"]))
        data = client.put(URL, json=body(months=12, keep_deleted=False, confirmed_messages=0), headers=auth(world["alice"])).json()
        assert (data["months"], data["keep_deleted"]) == (12, False)

    @pytest.mark.parametrize(
        "patch",
        [
            {"months": 0}, {"months": 121}, {"months": 6.5}, {"months": None},
            {"enabled": None}, {"keep_deleted": []},
            {"confirmed_messages": -1}, {"confirmed_messages": 2**63},
        ],
    )
    def test_invalid_values_are_refused_and_nothing_is_saved(self, client, world, patch):
        resp = client.put(URL, json=body(**patch), headers=auth(world["alice"]))
        assert resp.status_code == 422
        assert client.get(URL, headers=auth(world["alice"])).json()["configured"] is False


class TestIsolation:
    def test_one_accounts_policy_is_not_seen_by_another(self, client, world):
        client.put(URL, json=body(months=3, confirmed_messages=0), headers=auth(world["alice"]))
        assert client.get(URL, headers=auth(world["bob"])).json()["configured"] is False
        assert client.get(URL, headers=auth(world["alice"])).json()["months"] == 3

    def test_the_preview_counts_only_the_callers_archive(self, client, world):
        seed_aged(world["archives"]["alice"], [400, 400])
        assert client.post(PREVIEW, json={"months": 6}, headers=auth(world["bob"])).json()["total"] == 0


class TestUnmigratedArchive:
    @pytest.fixture
    def outdated(self, client, world):
        on_archive(world["archives"]["alice"], lambda c: (c.execute(sa.text("DROP TABLE retention_policy")), c.commit()))
        return world

    def test_reading_answers_409_archive_outdated(self, client, outdated):
        resp = client.get(URL, headers=auth(outdated["alice"]))
        assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_outdated"

    def test_saving_answers_409_archive_outdated_enabled_or_not(self, client, outdated):
        for payload in (body(), body(enabled=False, confirmed_messages=None)):
            resp = client.put(URL, json=payload, headers=auth(outdated["alice"]))
            assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "archive_outdated"

    def test_the_rest_of_the_api_keeps_working_afterwards(self, client, outdated):
        client.get(URL, headers=auth(outdated["alice"]))
        assert client.get("/api/chats", headers=auth(outdated["alice"])).status_code == 200


class TestConnections:
    def test_reads_use_the_read_only_connection(self):
        for fn in (archive_routes.get_retention, archive_routes.preview_retention):
            assert inspect.signature(fn).parameters["conn"].default.dependency is dependencies.get_archive_connection

    def test_the_save_uses_the_write_connection(self):
        assert inspect.signature(archive_routes.put_retention).parameters["conn"].default.dependency is dependencies.get_archive_write_connection
