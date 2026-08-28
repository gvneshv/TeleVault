"""Shared atomic-write helper for the small JSON status files this app polls from disk."""
import json
import time
from pathlib import Path


def atomic_write_json(path: Path, data: dict) -> None:
    """
    Write `data` to `path` as JSON, atomically - readers never see a half-written file.

    Windows quirk:
    unlike POSIX rename(), Windows' MoveFileEx (what os.replace() calls under the hood)
    can fail with PermissionError/WinError 5 if another process happens to have the destination file open for reading at that exact instant - which is common here,
    since the API polls this same file every few seconds.
    That's almost always resolved a few milliseconds later once the reader's handle closes,
    so retry a handful of times with a short pause instead of letting a transient conflict crash the whole process
    (which is exactly what happened - an unhandled PermissionError here took down an entire backfill run).
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    last_error = None
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.05 * (attempt + 1))
    raise last_error
