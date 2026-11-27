"""utils/media_files.py: deleting downloaded files safely - only this account's `users/<id>/media/<number>`, only what nothing refers to."""

import pytest

from utils.media_files import remove_media_files, resolve_media_file, sniff_media_type, sweep_orphans
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


class TestResolveMediaFile:
    def test_returns_the_file_of_this_account(self):
        target = put(5, "20")
        assert resolve_media_file(5, media_relative_path(5, 20)) == target

    @pytest.mark.parametrize("bad", [None, "", "../x", "users/5/media/../../x", "users/5/media/20.partial", "users/5/media/abc"])
    def test_wrong_shapes_resolve_to_nothing(self, bad):
        put(5, "20")
        assert resolve_media_file(5, bad) is None

    def test_another_accounts_path_resolves_to_nothing(self):
        put(6, "20")
        assert resolve_media_file(5, media_relative_path(6, 20)) is None

    def test_a_vanished_file_resolves_to_nothing(self):
        assert resolve_media_file(5, media_relative_path(5, 777)) is None


class TestSniffMediaType:
    @pytest.mark.parametrize(
        ("head", "expected", "ext"),
        [
            (b"\xff\xd8\xff\xe0" + b"0" * 12, "image/jpeg", "jpg"),
            (b"\x89PNG\r\n\x1a\n" + b"0" * 8, "image/png", "png"),
            (b"GIF89a" + b"0" * 10, "image/gif", "gif"),
            (b"RIFF\x00\x00\x00\x00WEBP", "image/webp", "webp"),
            (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav", "wav"),
            (b"\x00\x00\x00\x18ftypmp42", "video/mp4", "mp4"),
            (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4", "m4a"),
            (b"\x1a\x45\xdf\xa3" + b"0" * 12, "video/webm", "webm"),
            (b"OggS" + b"0" * 12, "audio/ogg", "ogg"),
            (b"ID3\x03" + b"0" * 12, "audio/mpeg", "mp3"),
            (b"%PDF-1.7" + b"0" * 8, "application/pdf", "pdf"),
        ],
    )
    def test_known_formats_are_inline(self, head, expected, ext):
        path = put(7, "1", head)
        assert sniff_media_type(path, "document") == (expected, ext, True)

    @pytest.mark.parametrize("body", [b"<html><script>alert(1)</script>", b"<svg onload=alert(1)>", b"MZ\x90\x00", b""])
    def test_anything_else_is_a_download(self, body):
        path = put(7, "2", body)
        assert sniff_media_type(path, "photo") == ("application/octet-stream", "bin", False)

    def test_a_missing_file_is_a_download(self):
        assert sniff_media_type(media_dir(7) / "nope", None) == ("application/octet-stream", "bin", False)