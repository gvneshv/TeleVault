"""utils/worker_paths.py and api/process_utils.py - the per-account heartbeat/status files that coordinate workers."""

import json
import os
import time
from pathlib import Path

import pytest

from api.process_utils import is_archiver_running, is_backfill_running, pid_alive, running_process_reason
from config import settings
from utils.worker_paths import backfill_status_path, heartbeat_path, user_dir

DEAD_PID = 2**31 - 5  # far above any real pid on Linux/macOS/Windows


class TestWorkerPaths:
    def test_paths_live_under_data_dir_per_account(self):
        assert user_dir(7) == Path(settings.data_dir) / "users" / "7"
        assert heartbeat_path(7).name == "televault.heartbeat"
        assert backfill_status_path(7).name == "backfill_status.json"

    def test_accounts_never_share_files(self):
        assert heartbeat_path(1) != heartbeat_path(2)
        assert backfill_status_path(1) != backfill_status_path(2)

    @pytest.mark.parametrize("hostile", ["../../etc", "1/../2", "1; rm -rf /", "", "abc"])
    def test_non_numeric_ids_cannot_escape_or_inject_into_the_path(self, hostile):
        with pytest.raises(ValueError):
            user_dir(hostile)  # type: ignore[arg-type]

    def test_numeric_strings_are_normalised_to_ints(self):
        assert user_dir("007") == user_dir(7)  # type: ignore[arg-type]


class TestPidAlive:
    def test_own_process_is_alive(self):
        assert pid_alive(os.getpid())

    def test_nonexistent_pid_is_not(self):
        assert not pid_alive(DEAD_PID)


class TestArchiverHeartbeat:
    def test_no_file_means_not_running(self, tmp_path):
        assert not is_archiver_running(tmp_path / "missing")

    def test_fresh_heartbeat_means_running(self, tmp_path):
        path = tmp_path / "hb"
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time()}))
        assert is_archiver_running(path)

    def test_stale_heartbeat_is_ignored_so_a_crash_cannot_block_a_restart(self, tmp_path):
        path = tmp_path / "hb"
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time() - 3600}))
        assert not is_archiver_running(path)

    def test_boundary_is_sixty_seconds(self, tmp_path):
        path = tmp_path / "hb"
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time() - 59}))
        assert is_archiver_running(path)
        path.write_text(json.dumps({"pid": 1, "updated_at": time.time() - 61}))
        assert not is_archiver_running(path)

    @pytest.mark.parametrize("content", ["", "not json", "{}", '{"updated_at": "soon"}', "[]"])
    def test_corrupt_heartbeat_counts_as_not_running(self, tmp_path, content):
        path = tmp_path / "hb"
        path.write_text(content)
        assert not is_archiver_running(path)


class TestBackfillStatus:
    def test_no_file(self, tmp_path):
        assert not is_backfill_running(tmp_path / "missing")

    def test_running_with_live_pid(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        assert is_backfill_running(path)

    def test_running_but_process_died_is_self_healing(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"state": "running", "pid": DEAD_PID}))
        assert not is_backfill_running(path)

    @pytest.mark.parametrize("state", ["completed", "cancelled", "error"])
    def test_finished_states_are_not_running_even_with_a_live_pid(self, tmp_path, state):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"state": state, "pid": os.getpid()}))
        assert not is_backfill_running(path)

    def test_missing_pid_and_corrupt_file(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"state": "running"}))
        assert not is_backfill_running(path)
        path.write_text("{broken")
        assert not is_backfill_running(path)


class TestRunningProcessReason:
    def test_nothing_running(self, tmp_path):
        assert running_process_reason(tmp_path / "hb", tmp_path / "bf") is None

    def test_archiver_wins_and_uses_the_established_reason_string(self, tmp_path):
        hb, bf = tmp_path / "hb", tmp_path / "bf"
        hb.write_text(json.dumps({"updated_at": time.time()}))
        bf.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        assert running_process_reason(hb, bf) == "already_running"

    def test_backfill_only(self, tmp_path):
        bf = tmp_path / "bf"
        bf.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        assert running_process_reason(tmp_path / "hb", bf) == "backfill_running"
