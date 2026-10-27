"""scripts/migrate_archives.py - brings every account's archive up to the current schema using only what the app already knows (CONTROL_DATABASE_URL)."""

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import db
from control_db import queries as cq
from db import provisioning
from scripts import migrate_archives

PREVIOUS_REVISION = "dd006ba3b06d"


def add_account(control_conn, username: str, archive: str | None) -> int:
    """A bare account row (the script reads nothing but id/username/archive_db_ref), optionally pointing at an archive database."""
    user_id = control_conn.execute(
        sa.text("INSERT INTO users (username, password_hash) VALUES (:u, 'x') RETURNING id"), {"u": username}
    ).scalar_one()
    control_conn.commit()
    if archive is not None:
        assert cq.set_archive_db_ref(control_conn, user_id, archive)
    return user_id


def make_old_archive(user_id: int) -> str:
    """A real archive taken back to the revision before the blocklist - what every archive created before that release looks like."""
    name = provisioning.provision_archive_database(user_id)
    engine = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE ingest_blocked_chats"))
        conn.execute(sa.text("UPDATE alembic_version SET version_num = :v"), {"v": PREVIOUS_REVISION})
    engine.dispose()
    return name


def has_blocklist_table(name: str) -> bool:
    engine = sa.create_engine(db.server_database_url(name), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT to_regclass('public.ingest_blocked_chats')")).scalar() is not None
    finally:
        engine.dispose()


@pytest.fixture
def accounts(control_conn, server_ready):
    return control_conn


def test_with_no_archives_there_is_nothing_to_do(accounts, capsys):
    add_account(accounts, "nodb", None)
    assert migrate_archives.main([]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_an_archive_that_is_already_current_is_left_alone(accounts, capsys):
    name = provisioning.provision_archive_database(900_020)
    add_account(accounts, "alice", name)
    assert migrate_archives.main([]) == 0
    out = capsys.readouterr().out
    assert "already up to date" in out and "migrated" not in out.split("archive(s) checked")[0]
    assert "0 migrated, 0 failed" in out


def test_dry_run_reports_an_outdated_archive_and_changes_nothing(accounts, capsys):
    name = make_old_archive(900_021)
    add_account(accounts, "alice", name)
    assert migrate_archives.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "OUTDATED" in out and PREVIOUS_REVISION in out and provisioning.head_revision() in out
    assert "Nothing was changed" in out
    assert not has_blocklist_table(name)
    assert provisioning.archive_revision(name) == PREVIOUS_REVISION


def test_an_outdated_archive_is_migrated_and_a_second_run_finds_nothing_to_do(accounts, capsys):
    name = make_old_archive(900_022)
    add_account(accounts, "alice", name)
    assert migrate_archives.main([]) == 0
    assert "migrated" in capsys.readouterr().out
    assert has_blocklist_table(name)
    assert provisioning.archive_revision(name) == provisioning.head_revision()

    assert migrate_archives.main([]) == 0
    out = capsys.readouterr().out
    assert "already up to date" in out and "0 migrated, 0 failed" in out


def test_one_broken_archive_does_not_stop_the_others_and_makes_the_exit_status_1(accounts, capsys):
    add_account(accounts, "ghost", "televault_test_does_not_exist")  # archive_db_ref pointing at a database that was never created
    good = make_old_archive(900_023)
    add_account(accounts, "alice", good)
    assert migrate_archives.main([]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "ghost" in out and "does not exist" in out
    assert "migrated" in out and has_blocklist_table(good)  # the healthy one was still done
    assert "1 migrated, 1 failed" in out


def test_the_report_never_contains_a_connection_url_or_credentials(accounts, capsys):
    add_account(accounts, "ghost", "televault_test_does_not_exist")
    add_account(accounts, "alice", make_old_archive(900_024))
    migrate_archives.main([])
    out = capsys.readouterr()
    for text in (out.out, out.err):
        assert "://" not in text and "password" not in text.lower()


def test_listing_archives_loads_no_secrets(accounts):
    name = provisioning.provision_archive_database(900_025)
    add_account(accounts, "alice", name)
    rows = cq.list_archive_databases(accounts)
    assert [set(r) for r in rows] == [{"id", "username", "archive_db_ref"}]
    assert rows[0]["archive_db_ref"] == name


def test_accounts_without_an_archive_are_not_listed(accounts):
    add_account(accounts, "nodb", None)
    assert cq.list_archive_databases(accounts) == []
