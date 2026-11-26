"""utils/media_files.py: deleting downloaded files safely - only this account's `users/<id>/media/<number>`, only what nothing refers to."""

import pytest

from utils.media_files import remove_media_files, sweep_orphans
from utils.worker_paths import media_dir, media_relative_path


def put(user_id: int, name: str, data: bytes = b"x"):
    folder = media_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


class TestRemoveMediaFiles:
    def test_removes_the_named_files_and_counts_them(self):
        a, b, keep = put(1, "10"), put(1, "11"), put(1, "12")
        assert remove_media_files(1, [media_relative_path(1, 10), media_relative_path(1, 11)]) == 2
        assert not a.exists() and not b.exists() and keep.exists()

    def test_a_file_that_is_already_gone_is_not_an_error_or_counted(self):
        assert remove_media_files(1, [media_relative_path(1, 999)]) == 0

    @pytest.mark.parametrize(
        "hostile",
        ["../../etc/passwd", "users/1/media/../../x", "users/1/media/12.partial", "users/1/media/abc", "/users/1/media/12", "", "users/1/other/12"],
    )
    def test_anything_but_the_exact_shape_is_ignored(self, hostile):
        victim = put(1, "12")
        assert remove_media_files(1, [hostile]) == 0
        assert victim.exists()

    def test_another_accounts_path_is_never_touched(self):
        theirs, mine = put(2, "5"), put(1, "5")
        assert remove_media_files(1, [media_relative_path(2, 5)]) == 0
        assert theirs.exists()
        assert mine.exists()  # a path that names another account must not be "re-aimed" at this account's folder either

    def test_an_empty_list_does_nothing(self):
        assert remove_media_files(1, []) == 0


class TestSweepOrphans:
    def test_removes_unreferenced_files_and_leftover_partials_only(self):
        kept, orphan, partial, foreign = put(3, "1"), put(3, "2"), put(3, "3.partial"), put(3, "notes.txt")
        assert sweep_orphans(3, {1}) == 2
        assert kept.exists() and foreign.exists()
        assert not orphan.exists() and not partial.exists()

    def test_no_media_folder_is_fine(self):
        assert sweep_orphans(987654, set()) == 0

    def test_another_accounts_folder_is_not_swept(self):
        theirs = put(4, "1")
        sweep_orphans(3, set())
        assert theirs.exists()