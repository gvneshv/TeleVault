"""
scripts/backup.py with the REAL tools: a PostgreSQL archive and control database are dumped through `age`,
verified with the private key, and restored into a scratch database.

Skipped (not failed) when `age`, `age-keygen`, `pg_dump` or `pg_restore` are not installed - they are system programs, not Python packages (RUNBOOK.md, "Backups").
The pipeline's failure handling needs none of them and is covered in tests/unit/test_backup.py.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import control_db
import db
from config import settings
from control_db import queries as cq
from db import provisioning
from tests.conftest import RUN_ID, TEST_DB_PREFIX, _create_database
from utils.archive_login import encrypt_archive_password

import scripts.backup as backup

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("age", "age-keygen", "pg_dump", "pg_restore")),
    reason="needs the age, age-keygen, pg_dump and pg_restore programs on PATH",
)

# A string that must appear in the database and must NOT appear anywhere in a stored backup file.
SECRET_MARKER = "deleted-message-text-that-must-not-leak"


@pytest.fixture
def keypair(tmp_path) -> tuple[str, Path]:
    """(public key, identity file) made by the real age-keygen - the private half stays in tmp_path, never given to backup.py (which takes the public key only)."""
    identity = tmp_path / "key.txt"
    result = subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True)
    public = (result.stdout + result.stderr).split("Public key:")[1].split()[0]
    return public, identity


@pytest.fixture
def secured_account(control_conn, server_ready):
    """An account whose archive was provisioned the production way: its own database AND its own login (so the backup must dump it as that login)."""
    user_id = control_conn.execute(sa.text("INSERT INTO users (username, password_hash) VALUES ('backup_user', 'x') RETURNING id")).scalar_one()
    control_conn.commit()
    provisioned = provisioning.provision_archive_database(user_id)
    assert cq.set_archive_db_ref(
        control_conn, user_id, provisioned.db_name, provisioned.login.role, encrypt_archive_password(provisioned.login.password)
    )
    engine = sa.create_engine(db.server_database_url(provisioned.db_name, provisioned.login), poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO chats (chat_id, name, chat_type) VALUES (4242, :t, 'private')"), {"t": SECRET_MARKER})
    engine.dispose()
    return provisioned


def restore_into_scratch(encrypted: Path, identity: Path, scratch: str) -> None:
    """age -d | pg_restore into a fresh scratch database, the way RUNBOOK.md's restore procedure does it."""
    _create_database(scratch)
    url = db.server_database_url(scratch)
    assert url.username is not None  # the test server URL always names a user; this also narrows the type for the type checker
    decrypt = subprocess.Popen(["age", "-d", "-i", str(identity), str(encrypted)], stdout=subprocess.PIPE)
    # Every argument is spelled as a plain str on purpose: subprocess.run()'s type stubs reject a `str | None` in the argument list.
    command: list[str] = ["pg_restore", "--no-owner", "--no-privileges", "-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username, "-d", scratch]
    environment: dict[str, str] = {"PGPASSWORD": url.password or "", "PATH": os.environ["PATH"]}
    restore = subprocess.run(command, stdin=decrypt.stdout, env=environment, capture_output=True)
    assert decrypt.stdout
    decrypt.stdout.close()
    assert decrypt.wait() == 0
    assert restore.returncode == 0, restore.stderr.decode()


def test_backup_verify_and_restore_round_trip(tmp_path, keypair, secured_account):
    public, identity = keypair
    out = tmp_path / "backups"

    assert backup.run_backup(out, [public], keep=3, docker_service=None) == 0

    (run_dir,) = list(out.iterdir())
    files = sorted(p.name for p in run_dir.iterdir())
    assert files == sorted(["control.dump.age", f"archive_{secured_account.db_name}.dump.age", backup.MANIFEST_NAME])

    # Nothing readable is stored: neither the message-like marker nor a plain-dump signature appears in any file.
    for path in run_dir.glob("*.age"):
        blob = path.read_bytes()
        assert SECRET_MARKER.encode() not in blob
        assert not blob.startswith(b"PGDMP")  # pg_dump -Fc's own magic number: would mean it was not encrypted
        assert blob.startswith(b"age-encryption.org/")

    assert backup.verify_backup(run_dir, identity) == 0

    # The data really comes back - the restore the RUNBOOK describes, into a throwaway database.
    scratch = f"{TEST_DB_PREFIX}{RUN_ID}_restore_check"
    restore_into_scratch(run_dir / f"archive_{secured_account.db_name}.dump.age", identity, scratch)
    engine = sa.create_engine(db.server_database_url(scratch), poolclass=NullPool)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT name FROM chats WHERE chat_id = 4242")).scalar_one() == SECRET_MARKER
    engine.dispose()


def test_verify_catches_a_flipped_byte_and_a_wrong_key(tmp_path, keypair, secured_account):
    public, identity = keypair
    out = tmp_path / "backups"
    assert backup.run_backup(out, [public], keep=3, docker_service=None) == 0
    (run_dir,) = list(out.iterdir())

    # A key that was not a recipient cannot open the files.
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    assert backup.verify_backup(run_dir, other) == 1

    # One changed byte: caught by the manifest before decryption is even attempted.
    target = run_dir / "control.dump.age"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    assert backup.verify_backup(run_dir, identity) == 1


def test_a_run_with_a_failing_archive_has_no_manifest_and_keeps_older_backups(tmp_path, keypair, secured_account, monkeypatch, capsys):
    public, _ = keypair
    out = tmp_path / "backups"
    old = out / "20200101T000000Z"
    old.mkdir(parents=True)
    (old / backup.MANIFEST_NAME).write_text("")  # an older, complete backup

    # The stored archive password no longer decrypts (what a changed FERNET_KEY looks like) - that archive must fail alone, loudly, without a fallback to the server login.
    engine = sa.create_engine(settings.control_database_url, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET archive_db_password = 'not-a-fernet-token' WHERE username = 'backup_user'"))
    engine.dispose()

    assert backup.run_backup(out, [public], keep=1, docker_service=None) == 1

    assert old.is_dir()  # rotation did not run, so the older good backup survived keep=1
    (new,) = [p for p in out.iterdir() if p != old]
    assert not (new / backup.MANIFEST_NAME).exists()
    assert (new / "control.dump.age").exists()  # the control database was still saved
    output = capsys.readouterr().out
    assert "FAILED" in output and "FERNET_KEY" in output
    assert "not-a-fernet-token" not in output  # never echoes what is stored


def test_two_runs_in_the_same_second_do_not_mix(tmp_path, keypair, secured_account, monkeypatch, capsys):
    public, _ = keypair
    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "run_dir_name", lambda now: "20261120T000000Z")
    assert backup.run_backup(out, [public], keep=3, docker_service=None) == 0
    before = {p.name: p.read_bytes() for p in (out / "20261120T000000Z").iterdir()}
    assert backup.run_backup(out, [public], keep=3, docker_service=None) == 1
    assert {p.name: p.read_bytes() for p in (out / "20261120T000000Z").iterdir()} == before
    assert "already exists" in capsys.readouterr().out