"""
Deleting downloaded files from the disk, for the rules that remove archived messages (cleanup, "delete this chat", "delete all data").

The database part of a removal (db/queries.py) hands back the stored `file_path` values of the rows it deleted;
the caller passes them here AFTER the database transaction committed.
That order matters: if the process dies between the two steps, the worst case is an unreferenced file (the archiver's start-up sweep removes it, see sweep_orphans) -
never a database row that points at a file that is gone.

Safety: a file_path comes from the database, but nothing outside `users/<this account>/media/<number>` is ever touched -
the exact shape utils.worker_paths.media_relative_path() writes.
Anything else (a corrupted row, a path with "..") is ignored and counted as not removed.
No name, path or content is ever logged, only counts.
"""

import logging
import re
from pathlib import Path
from typing import Iterable

from utils.worker_paths import media_dir

logger = logging.getLogger(__name__)

_RELATIVE_PATH = re.compile(r"^users/(\d+)/media/(\d+)$")
_FINAL_NAME = re.compile(r"^\d+$")
_PARTIAL_NAME = re.compile(r"^\d+\.partial$")


def remove_media_files(user_id: int, relative_paths: Iterable[str]) -> int:
    """
    Delete the given stored files of this account and return how many were actually removed (a file that is already gone is not an error and not counted).
    Never raises: a file that cannot be removed (permissions, in use) is logged by count and left for the start-up sweep.
    """
    removed = 0
    failed = 0
    folder = media_dir(user_id)
    for relative in relative_paths:
        match = _RELATIVE_PATH.match(relative or "")
        if match is None or int(match.group(1)) != int(user_id):
            failed += 1
            continue
        target = folder / match.group(2)
        try:
            target.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            failed += 1
    if failed:
        logger.warning("%d media file(s) could not be removed now; the archiver's next start will retry.", failed)
    return removed


def sweep_orphans(user_id: int, keep_media_ids: set[int]) -> int:
    """
    Delete files in this account's media folder that no stored row refers to, and leftover `.partial` downloads; return how many.
    `keep_media_ids` = the ids (message_media.id) of the rows with a stored file.

    Only for a moment when no download is running: the downloader calls it once before its first one.
    A file with any other name is left alone (it is not ours).
    Causes of orphans: a crash between commit and delete, a duplicate message dropped while merging a migrated chat, a download cut short.
    """
    folder = media_dir(user_id)
    if not folder.is_dir():
        return 0
    removed = 0
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if _FINAL_NAME.match(entry.name):
            orphan = int(entry.name) not in keep_media_ids
        else:
            orphan = bool(_PARTIAL_NAME.match(entry.name))
        if not orphan:
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def resolve_media_file(user_id: int, relative_path: str | None) -> Path | None:
    """
    The file on disk for a stored `file_path`, or None when the value has the wrong shape, names another account, or the file is gone.
    The same strict pattern as remove_media_files(): nothing outside `users/<this account>/media/<number>` is ever returned for serving.
    """
    match = _RELATIVE_PATH.match(relative_path or "")
    if match is None or int(match.group(1)) != int(user_id):
        return None
    target = media_dir(user_id) / match.group(2)
    return target if target.is_file() else None


# What the browser may DISPLAY inline.
# Everything else (documents, unknown formats, anything that could carry script such as HTML or SVG) is served as a plain download,
# so a hostile file archived from a chat can never run in the app's origin.
_INLINE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "application/pdf": "pdf",
}


def sniff_media_type(path: Path, kind: str | None) -> tuple[str, str, bool]:
    """
    Decide how to serve a stored file: (content_type, filename extension, inline).

    The type comes from the file's own first bytes, never from a name (the archive names files by number and does not keep the original name)
    and never from what the message claimed.
    `inline` is True only for the short allow-list above; otherwise the file is served as application/octet-stream to be downloaded.
    `kind` is a fallback only for the extension of an unrecognised file ("document" -> "bin").
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        head = b""
    detected: str | None = None
    if head.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        detected = "image/gif"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        detected = "image/webp"
    elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        detected = "audio/wav"
    elif head[4:8] == b"ftyp":
        # MP4 family: audio-only brands (M4A) are audio, the rest is video (QuickTime "qt  " plays as mp4 in browsers).
        detected = "audio/mp4" if head[8:12] in (b"M4A ", b"M4B ") else "video/mp4"
    elif head.startswith(b"\x1a\x45\xdf\xa3"):
        detected = "video/webm"
    elif head.startswith(b"OggS"):
        detected = "audio/ogg"
    elif head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        detected = "audio/mpeg"
    elif head.startswith(b"%PDF"):
        detected = "application/pdf"
    if detected in _INLINE_TYPES:
        return detected, _INLINE_TYPES[detected], True
    return "application/octet-stream", "bin", False