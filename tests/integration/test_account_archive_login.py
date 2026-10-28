"""A worker (main.py, backfill.py) learns its archive's own login from utils/account.py - and refuses to start rather than use the wrong one."""

import pytest
import sqlalchemy as sa

import db
from control_db import queries as cq
from db import provisioning
from utils.account import AccountContextError, load_account_context
from utils.archive_login import encrypt_archive_password
from utils.crypto import encrypt_secret


@pytest.fixture
def linked_account(control_conn, server_ready):
    """An account with Telegram linked and its own, really provisioned, archive. Returns (user_id, provisioned archive)."""
    user_id = control_conn.execute(
        sa.text(
            "INSERT INTO users (username, password_hash, telegram_api_id, telegram_api_hash, telegram_session_string) "
            "VALUES ('worker', 'x', :i, :h, :s) RETURNING id"
        ),
        {"i": encrypt_secret("12345"), "h": encrypt_secret("hash"), "s": encrypt_secret("session")},
    ).scalar_one()
    control_conn.commit()
    archive = provisioning.provision_archive_database(900_060)
    assert cq.set_archive_db_ref(control_conn, user_id, archive.db_name, archive.login.role, encrypt_archive_password(archive.login.password))
    return user_id, archive


def test_the_worker_gets_the_archives_own_login(linked_account):
    user_id, archive = linked_account
    account = load_account_context(user_id)
    assert account.archive_db_name == archive.db_name
    assert account.archive_login == archive.login


def test_the_workers_database_url_authenticates_as_that_role(linked_account):
    """This is exactly what main.py / backfill.py pass to db.init_db()."""
    user_id, archive = linked_account
    account = load_account_context(user_id)
    url = db.server_database_url(account.archive_db_name, account.archive_login)
    assert url.username == archive.login.role


def test_the_context_never_prints_a_password(linked_account):
    user_id, archive = linked_account
    assert archive.login.password not in repr(load_account_context(user_id))


def test_an_archive_without_its_own_login_has_none(linked_account, control_conn):
    user_id, archive = linked_account
    assert cq.set_archive_db_ref(control_conn, user_id, archive.db_name)  # no login given: the pre-per-archive-role state
    assert load_account_context(user_id).archive_login is None


def test_an_undecryptable_login_stops_the_worker_instead_of_using_the_server_login(linked_account, control_conn):
    user_id, _ = linked_account
    control_conn.execute(sa.text("UPDATE users SET archive_db_password = 'garbage' WHERE id = :i"), {"i": user_id})
    control_conn.commit()
    with pytest.raises(AccountContextError) as excinfo:
        load_account_context(user_id)
    assert excinfo.value.reason == "archive_login_unreadable"
    assert "garbage" not in str(excinfo.value)
