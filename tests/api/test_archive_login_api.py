"""
The API side of per-archive logins: provisioning stores the archive's own login (encrypted), every request opens the archive AS that login,
an unusable stored login is an error and never a silent fall-back to the all-powerful server login, and deleting an account removes its role.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
from api import dependencies
from config import settings
from control_db import queries as cq
from tests.helpers import auth, make_admin, make_user


def provision(client, token) -> str:
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["archive_db_ref"]


def control_scalar(sql: str, **params):
    engine = sa.create_engine(settings.control_database_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def role_exists(role: str) -> bool:
    return control_scalar("SELECT COUNT(*) FROM pg_roles WHERE rolname = :r", r=role) == 1


@pytest.fixture
def alice(client):
    admin = make_admin(client)
    token, uid = make_user(client, admin, "alice")
    return {"admin": admin, "token": token, "id": uid, "ref": provision(client, token)}


class TestProvisioningStoresTheLogin:
    def test_the_account_records_the_role_and_an_encrypted_password(self, client, alice):
        row = control_scalar("SELECT archive_db_user || '|' || archive_db_password FROM users WHERE id = :i", i=alice["id"])
        assert row is not None
        role, stored = row.split("|", 1)
        assert role == alice["ref"]  # role and database share a name
        assert stored.startswith("gAAAA")  # a Fernet token, not a password

    def test_the_response_never_contains_a_password(self, client):
        admin = make_admin(client)
        token, _ = make_user(client, admin, "bob")
        resp = client.post("/api/archive/provision", headers=auth(token))
        assert set(resp.json()) == {"archive_db_ref"}

    def test_the_stored_password_really_opens_the_archive(self, client, alice, control_conn):
        from db import provisioning
        from utils.archive_login import archive_login_from_row

        row = cq.get_archive_login_row(control_conn, alice["id"])
        assert row is not None
        login = archive_login_from_row(row)
        assert login is not None
        provisioning.verify_archive_login(alice["ref"], login)


class TestRequestsUseTheArchivesOwnLogin:
    def test_the_connection_a_request_gets_is_the_archives_own_role(self, client, alice, control_conn):
        """The read dependency and the write dependency must both authenticate as the archive's role - not as the server login."""
        for dependency in (dependencies.get_archive_connection, dependencies.get_archive_write_connection):
            gen = dependency(current={"user_id": alice["id"]}, control_conn=control_conn)  # type: ignore[arg-type]
            conn = next(gen)
            try:
                user, database = conn.execute(sa.text("SELECT current_user, current_database()")).one()
            finally:
                gen.close()
            assert (user, database) == (alice["ref"], alice["ref"]), dependency.__name__

    def test_two_accounts_each_get_their_own_role(self, client, alice, control_conn):
        token, bob_id = make_user(client, alice["admin"], "bob")
        bob_ref = provision(client, token)
        gen = dependencies.get_archive_connection(current={"user_id": bob_id}, control_conn=control_conn)  # type: ignore[arg-type]
        conn = next(gen)
        try:
            assert conn.execute(sa.text("SELECT current_user")).scalar() == bob_ref != alice["ref"]
        finally:
            gen.close()

    def test_an_archive_without_its_own_login_still_works_the_way_it_always_did(self, client, alice):
        """Archives created before per-archive logins have no stored login until scripts/secure_archives.py runs - they must not break in between."""
        control_scalar("UPDATE users SET archive_db_user = NULL, archive_db_password = NULL WHERE id = :i", i=alice["id"])
        assert client.get("/api/chats", headers=auth(alice["token"])).status_code == 200


class TestAnUnusableStoredLoginIsNeverReplacedByTheServerLogin:
    @pytest.fixture
    def broken(self, client, alice):
        control_scalar("UPDATE users SET archive_db_password = 'not-a-fernet-token' WHERE id = :i", i=alice["id"])
        return alice

    def test_reads_and_writes_answer_503_not_200(self, client, broken):
        """Falling back to the server login here would work - and quietly remove the isolation. It must fail instead."""
        assert client.get("/api/chats", headers=auth(broken["token"])).status_code == 503
        assert client.put("/api/ingest/blocklist/1", headers=auth(broken["token"])).status_code == 503

    def test_the_response_says_unavailable_and_leaks_nothing(self, client, broken):
        resp = client.get("/api/chats", headers=auth(broken["token"]))
        assert resp.json()["detail"]["reason"] == "archive_unavailable"
        assert "not-a-fernet-token" not in resp.text and "FERNET" not in resp.text

    def test_the_health_page_shows_unavailable_instead_of_failing(self, client, broken):
        resp = client.get("/api/health", headers=auth(broken["token"]))
        assert resp.status_code == 200 and resp.json()["archive_status"] == "unavailable"

    def test_a_half_written_login_is_unusable_too(self, client, alice):
        control_scalar("UPDATE users SET archive_db_password = NULL WHERE id = :i", i=alice["id"])
        assert client.get("/api/chats", headers=auth(alice["token"])).status_code == 503


class TestDeletingAnAccount:
    def test_its_role_is_dropped_with_its_database(self, client, alice):
        assert role_exists(alice["ref"])
        assert client.delete(f"/api/admin/users/{alice['id']}", headers=auth(alice["admin"])).status_code == 200
        assert not role_exists(alice["ref"])

    def test_an_account_without_a_role_drops_no_role(self, client, alice):
        """The role is only ever dropped by the name STORED on the account - never guessed from the database name."""
        control_scalar("UPDATE users SET archive_db_user = NULL, archive_db_password = NULL WHERE id = :i", i=alice["id"])
        assert client.delete(f"/api/admin/users/{alice['id']}", headers=auth(alice["admin"])).status_code == 200
        assert role_exists(alice["ref"])  # left behind on purpose; a harmless orphan beats dropping a role nobody recorded
        control_scalar(f'DROP ROLE "{alice["ref"]}"')


class TestControlQueries:
    def test_pointing_the_account_at_another_database_clears_the_old_login(self, client, alice, control_conn):
        assert cq.set_archive_db_ref(control_conn, alice["id"], "some_other_db")
        row = cq.get_archive_login_row(control_conn, alice["id"])
        assert row == {"archive_db_ref": "some_other_db", "archive_db_user": None, "archive_db_password": None}

    def test_a_user_without_a_password_is_refused(self, client, alice, control_conn):
        with pytest.raises(ValueError):
            cq.set_archive_db_ref(control_conn, alice["id"], "x", archive_db_user="r")
        row = cq.get_archive_login_row(control_conn, alice["id"])
        assert row is not None and row["archive_db_ref"] == alice["ref"]  # nothing was written

    def test_a_login_cannot_be_set_for_an_account_with_no_archive(self, client, control_conn):
        admin = make_admin(client)
        _, uid = make_user(client, admin, "carol")  # never provisioned
        assert cq.set_archive_db_login(control_conn, uid, "r", "p") is False
        assert cq.get_archive_login_row(control_conn, uid) == {"archive_db_ref": None, "archive_db_user": None, "archive_db_password": None}

    def test_unknown_account(self, control_conn):
        assert cq.get_archive_login_row(control_conn, 424242) is None
        assert cq.set_archive_db_login(control_conn, 424242, "r", "p") is False
