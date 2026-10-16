"""main.py and backfill.py refuse to start when this account's Telegram session is already in use."""

import json
import os
import time

import pytest

import backfill
import main
from utils.worker_paths import backfill_status_path, heartbeat_path

USER = 987001  # an id no other test uses; its files live under the throwaway DATA_DIR


@pytest.fixture(autouse=True)
def _clean_files():
    yield
    for path in (heartbeat_path(USER), backfill_status_path(USER)):
        path.unlink(missing_ok=True)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _live_heartbeat():
    _write(heartbeat_path(USER), {"pid": os.getpid(), "updated_at": time.time()})


def _live_backfill():
    _write(backfill_status_path(USER), {"state": "running", "pid": os.getpid()})


class TestArchiverStartGuard:
    def test_clean_start_is_allowed(self):
        main._refuse_if_already_running(USER)  # must simply return

    def test_second_archiver_for_the_same_account_is_refused(self, caplog):
        _live_heartbeat()
        with pytest.raises(SystemExit) as exc:
            main._refuse_if_already_running(USER)
        assert exc.value.code == 1
        assert "already running" in caplog.text

    def test_running_backfill_blocks_the_archiver(self, caplog):
        _live_backfill()
        with pytest.raises(SystemExit) as exc:
            main._refuse_if_already_running(USER)
        assert exc.value.code == 1
        assert "backfill is currently running" in caplog.text

    def test_a_stale_heartbeat_does_not_block(self):
        _write(heartbeat_path(USER), {"pid": 1, "updated_at": time.time() - 600})
        main._refuse_if_already_running(USER)

    def test_another_accounts_worker_is_none_of_our_business(self):
        _write(heartbeat_path(USER + 1), {"pid": os.getpid(), "updated_at": time.time()})
        try:
            main._refuse_if_already_running(USER)
        finally:
            heartbeat_path(USER + 1).unlink(missing_ok=True)


class TestBackfillStartGuard:
    def test_clean_start_is_allowed(self):
        backfill._refuse_if_conflicting(USER)

    def test_second_backfill_for_the_same_account_is_refused(self, caplog):
        _live_backfill()
        with pytest.raises(SystemExit) as exc:
            backfill._refuse_if_conflicting(USER)
        assert exc.value.code == 1
        assert "backfill is already running" in caplog.text

    def test_live_archiver_blocks_the_backfill(self, caplog):
        _live_heartbeat()
        with pytest.raises(SystemExit) as exc:
            backfill._refuse_if_conflicting(USER)
        assert exc.value.code == 1
        assert "live userbot" in caplog.text
