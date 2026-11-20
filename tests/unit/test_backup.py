"""
scripts/backup.py - the rules that need no database: key validation, file naming, command lines, manifest, rotation, and the pg_dump -> age pipeline's failure handling.

The pipeline tests swap the two real programs for tiny Python one-liners (sys.executable, so they run the same on Windows and Linux and need neither age nor PostgreSQL).
The real tools are exercised in tests/integration/test_backup_roundtrip.py.
"""

import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.engine import make_url

import scripts.backup as backup

PUBLIC_KEY = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"  # shape of a real one; never used to encrypt anything here


class TestRecipients:
    def test_a_public_key_is_accepted_unchanged(self):
        assert backup.check_recipients([PUBLIC_KEY]) == [PUBLIC_KEY]

    def test_blank_entries_are_dropped_but_an_empty_result_is_refused(self):
        assert backup.check_recipients(["  ", PUBLIC_KEY, ""]) == [PUBLIC_KEY]
        with pytest.raises(backup.BackupError, match="No age recipient"):
            backup.check_recipients(["", "   "])

    def test_a_private_key_is_refused_with_a_clear_reason(self):
        # The one mistake that defeats the whole design: the secret that opens every backup placed on the server.
        with pytest.raises(backup.BackupError, match="PRIVATE key"):
            backup.check_recipients(["AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"])

    @pytest.mark.parametrize("value", ["ssh-ed25519 AAAAC3Nza", "hunter2", "ag1abc"])
    def test_anything_that_is_not_an_age_public_key_is_refused(self, value):
        with pytest.raises(backup.BackupError, match="Not an age public key"):
            backup.check_recipients([value])


class TestNaming:
    def test_run_folder_is_utc_sortable_and_has_no_colon(self):
        kyiv = timezone(timedelta(hours=2))
        name = backup.run_dir_name(datetime(2026, 11, 20, 2, 7, 5, tzinfo=kyiv))
        assert name == "20261120T000705Z"
        assert ":" not in name

    def test_file_names(self):
        assert backup.backup_file_name("control", "televault_control") == "control.dump.age"
        assert backup.backup_file_name("archive", "televault_archive_3") == "archive_televault_archive_3.dump.age"

    @pytest.mark.parametrize("name", ["../evil", "a/b", "a b", "", "x" * 64, "db;rm", "ü"])
    def test_a_database_name_that_is_not_a_plain_identifier_never_becomes_a_path(self, name):
        with pytest.raises(backup.BackupError):
            backup.backup_file_name("archive", name)


class TestCommands:
    def test_pg_dump_on_the_host_carries_host_and_port_but_no_password(self):
        url = make_url("postgresql+psycopg2://televault:topsecret@db.example:5433/televault_control")
        command = backup.pg_dump_command(url, None)
        assert command[:2] == ["pg_dump", "-Fc"]
        assert ["-h", "db.example"] == command[command.index("-h"): command.index("-h") + 2]
        assert ["-p", "5433"] == command[command.index("-p"): command.index("-p") + 2]
        assert "topsecret" not in " ".join(command)

    def test_pg_dump_in_docker_passes_the_password_by_environment_only(self):
        url = make_url("postgresql+psycopg2://televault:topsecret@localhost:5432/televault_control")
        command = backup.pg_dump_command(url, "postgres")
        assert command[:6] == ["docker", "compose", "exec", "-T", "-e", "PGPASSWORD"]
        assert "postgres" in command and "-h" not in command  # the host's view of the server is wrong from inside the container
        assert "topsecret" not in " ".join(command)

    def test_the_password_reaches_the_child_through_the_environment_only(self):
        url = make_url("postgresql+psycopg2://u:topsecret@h/d")
        assert backup._pg_env(url)["PGPASSWORD"] == "topsecret"

    def test_a_stale_pgpassword_from_the_shell_is_not_tried_against_another_database(self, monkeypatch):
        monkeypatch.setenv("PGPASSWORD", "from-the-shell")
        assert "PGPASSWORD" not in backup._pg_env(make_url("postgresql+psycopg2://u@h/d"))

    def test_age_command_has_one_flag_per_recipient_and_no_decrypt_option(self):
        command = backup.age_encrypt_command([PUBLIC_KEY, PUBLIC_KEY.replace("age1q", "age1w", 1)])
        assert command.count("-r") == 2
        assert "-d" not in command and "-i" not in command  # the server-side command can never decrypt


class TestManifest:
    def test_round_trip_and_sorted_output(self):
        hashes = {"b.dump.age": "b" * 64, "a.dump.age": "a" * 64}
        text = backup.manifest_text(hashes)
        assert text == f"{'a' * 64}  a.dump.age\n{'b' * 64}  b.dump.age\n"
        assert backup.parse_manifest(text) == hashes

    @pytest.mark.parametrize("line", ["nonsense", f"{'a' * 63}  short.age", f"{'a' * 64}  ../escape.age", f"{'A' * 64}  upper.age"])
    def test_a_damaged_or_path_like_line_is_rejected(self, line):
        with pytest.raises(backup.BackupError, match="damaged"):
            backup.parse_manifest(line + "\n")


class TestRotation:
    NAMES = ["20261101T000000Z", "20261102T000000Z", "20261103T000000Z", "20261104T000000Z"]

    def test_the_oldest_complete_runs_beyond_keep_are_chosen(self):
        assert backup.runs_to_delete(self.NAMES, set(self.NAMES), keep=2) == self.NAMES[:2]

    def test_nothing_is_deleted_while_within_keep(self):
        assert backup.runs_to_delete(self.NAMES, set(self.NAMES), keep=4) == []

    def test_a_folder_without_a_manifest_is_never_deleted_and_does_not_count_as_a_backup(self):
        # 20261104 is unfinished (maybe still running). It must survive, and must not push a good older backup out of the "keep" window.
        complete = set(self.NAMES[:3])
        assert backup.runs_to_delete(self.NAMES, complete, keep=3) == []

    def test_folders_this_script_did_not_make_are_left_alone(self):
        existing = [*self.NAMES, "my-own-notes", "20261105"]
        assert backup.runs_to_delete(existing, set(existing), keep=1) == self.NAMES[:3]


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


COPY_STDIN_TO_STDOUT = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


class TestDumpEncryptedPipeline:
    """pg_dump's stdout becomes age's stdin, age's stdout becomes the file; a failure of EITHER must leave no file that could pass for a backup."""

    URL = make_url("postgresql+psycopg2://u:p@h/d")

    def run(self, tmp_path: Path, dump_code: str, age_code: str) -> Path:
        destination = tmp_path / "x.dump.age"
        with mock.patch.object(backup, "pg_dump_command", return_value=_python(dump_code)), \
             mock.patch.object(backup, "age_encrypt_command", return_value=_python(age_code)):
            backup.dump_encrypted(self.URL, destination, [PUBLIC_KEY], None)
        return destination

    def test_success_writes_exactly_what_the_pipeline_produced(self, tmp_path):
        destination = self.run(tmp_path, "import sys; sys.stdout.write('payload')", COPY_STDIN_TO_STDOUT)
        assert destination.read_bytes() == b"payload"
        assert not list(tmp_path.glob("*.partial"))

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_file_is_owner_only(self, tmp_path):
        destination = self.run(tmp_path, "import sys; sys.stdout.write('payload')", COPY_STDIN_TO_STDOUT)
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    def test_a_failing_pg_dump_leaves_nothing_even_if_age_succeeded(self, tmp_path):
        # age exits 0 on whatever it was given (here: a truncated dump), so the pg_dump status is the only thing that can catch this.
        with pytest.raises(backup.BackupError, match="pg_dump failed: connection refused"):
            self.run(tmp_path, "import sys; sys.stdout.write('half'); sys.stderr.write('connection refused'); sys.exit(1)", COPY_STDIN_TO_STDOUT)
        assert list(tmp_path.iterdir()) == []

    def test_a_failing_age_leaves_nothing(self, tmp_path):
        with pytest.raises(backup.BackupError, match="age failed: bad key"):
            self.run(tmp_path, "import sys; sys.stdout.write('data')", "import sys; sys.stderr.write('bad key'); sys.exit(1)")
        assert list(tmp_path.iterdir()) == []

    def test_an_empty_result_is_not_a_backup(self, tmp_path):
        with pytest.raises(backup.BackupError, match="empty"):
            self.run(tmp_path, "pass", COPY_STDIN_TO_STDOUT)
        assert list(tmp_path.iterdir()) == []

    def test_a_missing_program_is_reported_by_name(self, tmp_path):
        with mock.patch.object(backup, "pg_dump_command", return_value=["definitely-not-installed-xyz"]):
            with pytest.raises(backup.BackupError, match="definitely-not-installed-xyz"):
                backup.dump_encrypted(self.URL, tmp_path / "x.dump.age", [PUBLIC_KEY], None)
        assert list(tmp_path.iterdir()) == []

    def test_the_error_shown_never_contains_the_password(self, tmp_path):
        with pytest.raises(backup.BackupError) as caught:
            self.run(tmp_path, "import sys; sys.stderr.write('could not connect'); sys.exit(2)", COPY_STDIN_TO_STDOUT)
        assert str(caught.value) == "pg_dump failed: could not connect"  # the tool's own words, nothing of the connection URL


class TestMainArguments:
    def test_keep_below_one_is_refused_before_anything_runs(self):
        with pytest.raises(SystemExit):
            backup.main(["--recipient", PUBLIC_KEY, "--keep", "0"])

    def test_verify_needs_an_identity(self):
        with pytest.raises(SystemExit):
            backup.main(["--verify", "somewhere"])

    def test_no_recipient_anywhere_stops_before_touching_a_database(self, monkeypatch, capsys):
        monkeypatch.delenv(backup.RECIPIENT_ENV, raising=False)
        with mock.patch.object(backup, "run_backup") as run:
            assert backup.main([]) == 1
        run.assert_not_called()
        assert "No age recipient" in capsys.readouterr().out

    def test_the_environment_variable_supplies_recipients(self, monkeypatch):
        monkeypatch.setenv(backup.RECIPIENT_ENV, PUBLIC_KEY)
        with mock.patch.object(backup, "run_backup", return_value=0) as run:
            assert backup.main([]) == 0
        assert run.call_args.args[1] == [PUBLIC_KEY]


class TestVerifyNeedsNoApplicationSettings:
    def test_importing_the_script_does_not_load_config(self):
        # `--verify` runs on the machine that holds the private key, which has no .env.
        # config.py exits when FERNET_KEY / JWT_SECRET are missing, so a top-level `import config` would make verification impossible exactly where it is meant to run.
        # A fresh interpreter: this test process has already imported config (conftest), so sys.modules here proves nothing.
        import subprocess

        repo = Path(__file__).resolve().parents[2]
        code = f"import sys; sys.path.insert(0, {str(repo)!r}); import scripts.backup; assert 'config' not in sys.modules, 'config was imported'"
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr