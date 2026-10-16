"""Small helpers shared by the API/integration tests (kept out of conftest.py so they can be imported normally)."""

from tests.conftest import BOOTSTRAP_TOKEN

PASSWORD = "correct horse battery"


def auth(token: str) -> dict[str, str]:
    """Authorization header for a bearer access token."""
    return {"Authorization": f"Bearer {token}"}


def register(client, invite_token: str, username: str, password: str = PASSWORD, accepted_terms: bool = True):
    """POST /api/auth/register - returns the raw response so tests can assert on failures too."""
    return client.post(
        "/api/auth/register",
        json={"invite_token": invite_token, "username": username, "password": password, "accepted_terms": accepted_terms},
    )


def login(client, username: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def make_admin(client, username: str = "admin") -> str:
    """Become the instance's admin through the real bootstrap-token path; returns the access token."""
    resp = register(client, BOOTSTRAP_TOKEN, username)
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def make_invite(client, admin_token: str) -> str:
    resp = client.post("/api/admin/invites", headers=auth(admin_token), json={})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["token"]


def make_user(client, admin_token: str, username: str) -> tuple[str, int]:
    """Create an ordinary account through a real invite; returns (access_token, user_id)."""
    resp = register(client, make_invite(client, admin_token), username)
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    me = client.get("/api/auth/me", headers=auth(token))
    assert me.status_code == 200, me.text
    return token, me.json()["id"]


# ---------------------------------------------------------------------------
# Archive seeding (used by the integration and API tests)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def seed_chat(conn, chat_id: int, name: str, chat_type: str = "private", messages=(), deleted=(), edited=None, self_id=None):
    """
    Write one chat into an archive through the SAME functions the workers use (db/queries.py).

    messages: iterable of (tg_message_id, sender_id, text); dates are T0 + i minutes so ordering is predictable.
    deleted:  tg_message_ids to flag as deleted afterwards.
    edited:   {tg_message_id: new_text} applied afterwards.
    """
    import db.queries as q

    q.upsert_chat(conn, chat_id=chat_id, name=name, chat_type=chat_type, username=None)
    for i, (tg_id, sender_id, text) in enumerate(messages):
        if sender_id is not None:
            q.upsert_sender(conn, sender_id=sender_id, username=f"user{sender_id}", first_name=f"User{sender_id}", last_name=None)
        q.insert_message(conn, tg_message_id=tg_id, chat_id=chat_id, sender_id=sender_id, text=text, date=T0 + timedelta(minutes=i))
    for tg_id, new_text in (edited or {}).items():
        q.record_edit(conn, tg_message_id=tg_id, chat_id=chat_id, new_text=new_text)
    for tg_id in deleted:
        q.flag_deleted(conn, tg_message_id=tg_id, chat_id=chat_id, self_id=self_id)


def seed_archive_by_name(db_name: str, **kwargs):
    """seed_chat() on a fresh connection to a named archive database (for tests that provisioned it through the API)."""
    import db

    eng = sa.create_engine(db.server_database_url(db_name), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            seed_chat(conn, **kwargs)
    finally:
        eng.dispose()
