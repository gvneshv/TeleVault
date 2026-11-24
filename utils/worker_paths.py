"""
Where each account's runtime files live.

TeleVault runs one archiver (main.py) and, on demand, one backfill (backfill.py) PER ACCOUNT, as separate subprocesses spawned by the API server.
Those processes and the API server never talk to each other directly;
they coordinate through a few small files per account:

    <data_dir>/users/<user_id>/televault.heartbeat    written every few seconds by main.py while it is connected
    <data_dir>/users/<user_id>/backfill_status.json   written by backfill.py as it progresses
    <data_dir>/users/<user_id>/dialogs.json           the account's Telegram chat list, written by main.py (dialog_catalog.py) for the backfill's chat picker

and, since media archiving, the account's downloaded files:

    <data_dir>/users/<user_id>/media/<message_media.id>   one file per stored attachment (media_downloader.py)

Keying the files by user_id (rather than one instance-wide pair, as the single-user version did) is what lets several accounts archive at the same time,
and what makes every "is something running?" check answer for the right account only.

All paths are absolute - config.py resolves settings.data_dir against the repo root at load time,
so the API server and the workers it spawns agree on them no matter which directory each was started from.
"""

from pathlib import Path

from config import settings


def user_dir(user_id: int) -> Path:
    """The directory holding every runtime file for one account.
    Not created here - writers create it when they first write."""
    return Path(settings.data_dir) / "users" / str(int(user_id))


def heartbeat_path(user_id: int) -> Path:
    """Heartbeat file of this account's live archiver (main.py).
    See api/process_utils.is_archiver_running() for how staleness is judged."""
    return user_dir(user_id) / "televault.heartbeat"


def backfill_status_path(user_id: int) -> Path:
    """Status file of this account's backfill (backfill.py).
    See api/process_utils.is_backfill_running()."""
    return user_dir(user_id) / "backfill_status.json"


def dialogs_path(user_id: int) -> Path:
    """Chat-list snapshot of this account, written by its live archiver (dialog_catalog.py) and read by the API for the backfill's chat picker."""
    return user_dir(user_id) / "dialogs.json"


def media_dir(user_id: int) -> Path:
    """The directory holding this account's downloaded attachments.
    Not created here - the downloader creates it on first use."""
    return user_dir(user_id) / "media"


def media_relative_path(user_id: int, media_id: int) -> str:
    """
    What goes into message_media.file_path: the file's location RELATIVE to the data directory, with forward slashes on every OS.
    Relative so the archive stays valid when DATA_DIR moves (a new disk, a restored backup), and so a database row can never point outside DATA_DIR.
    Named by the row's own id, never by anything Telegram supplied: no file name from a stranger ever reaches the file system.
    """
    return f"users/{int(user_id)}/media/{int(media_id)}"