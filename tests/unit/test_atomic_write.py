"""utils/atomic_write.py - crash-safe JSON writes with the Windows PermissionError retry."""

import json
import pathlib
from unittest import mock

import pytest

from utils.atomic_write import atomic_write_json


def test_writes_json_and_creates_parent_directories(tmp_path):
    target = tmp_path / "nested" / "dir" / "status.json"
    atomic_write_json(target, {"state": "running", "n": 1})
    assert json.loads(target.read_text()) == {"state": "running", "n": 1}


def test_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "status.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}


def test_leaves_no_temp_file_behind_on_success(tmp_path):
    atomic_write_json(tmp_path / "status.json", {})
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_retries_a_transient_permission_error(tmp_path):
    calls = {"n": 0}
    real_replace = pathlib.Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("WinError 5: file is open in another process")
        return real_replace(self, target)

    with mock.patch.object(pathlib.Path, "replace", flaky), mock.patch("time.sleep") as sleep:
        atomic_write_json(tmp_path / "status.json", {"ok": True})
    assert calls["n"] == 3
    assert sleep.call_count == 2
    assert json.loads((tmp_path / "status.json").read_text()) == {"ok": True}


def test_gives_up_after_five_attempts_and_raises_the_original_error(tmp_path):
    with mock.patch.object(pathlib.Path, "replace", side_effect=PermissionError("locked")) as replace, mock.patch("time.sleep"):
        with pytest.raises(PermissionError, match="locked"):
            atomic_write_json(tmp_path / "status.json", {})
    assert replace.call_count == 5


def test_other_errors_are_not_retried(tmp_path):
    with mock.patch.object(pathlib.Path, "replace", side_effect=OSError("disk full")) as replace:
        with pytest.raises(OSError, match="disk full"):
            atomic_write_json(tmp_path / "status.json", {})
    assert replace.call_count == 1
