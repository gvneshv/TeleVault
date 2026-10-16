"""POST /api/archive/provision - each account creates its own archive database."""

from sqlalchemy import text

from db.provisioning import ArchiveProvisioningError
from tests.api.test_admin_api import db_exists
from tests.helpers import auth, make_admin, make_user


def test_provisioning_creates_a_migrated_database_and_records_it(client, control_engine):
    admin = make_admin(client)
    token, uid = make_user(client, admin, "alice")
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code in (200, 201)
    ref = resp.json()["archive_db_ref"]
    assert db_exists(ref)
    with control_engine.connect() as conn:
        assert conn.execute(text("SELECT archive_db_ref FROM users WHERE id = :i"), {"i": uid}).scalar() == ref
    stats = client.get("/api/stats", headers=auth(token))
    assert stats.status_code == 200 and stats.json()["total_messages"] == 0  # the schema is really there


def test_a_second_call_is_refused_and_changes_nothing(client):
    admin = make_admin(client)
    token, _ = make_user(client, admin, "alice")
    first = client.post("/api/archive/provision", headers=auth(token)).json()["archive_db_ref"]
    again = client.post("/api/archive/provision", headers=auth(token))
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "archive_already_provisioned"
    assert client.get("/api/health", headers=auth(token)).json()["archive_status"] == "ok"
    assert db_exists(first)


def test_every_account_gets_its_own_database(client):
    admin = make_admin(client)
    refs = {client.post("/api/archive/provision", headers=auth(t)).json()["archive_db_ref"] for t in (admin, make_user(client, admin, "alice")[0], make_user(client, admin, "bob")[0])}
    assert len(refs) == 3


def test_a_provisioning_failure_is_a_503_with_a_reason_and_leaves_the_account_unattached(client, monkeypatch):
    import api.routes.archive as route

    def boom(user_id):
        raise ArchiveProvisioningError("no CREATEDB", "provisioning_permission_denied")

    monkeypatch.setattr(route, "provision_archive_database", boom)
    admin = make_admin(client)
    token, _ = make_user(client, admin, "alice")
    resp = client.post("/api/archive/provision", headers=auth(token))
    assert resp.status_code == 503 and resp.json()["detail"]["reason"] == "provisioning_permission_denied"
    assert client.get("/api/health", headers=auth(token)).json()["archive_status"] == "unattached"
