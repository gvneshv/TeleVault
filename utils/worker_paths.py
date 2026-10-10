"""
Where each account's runtime files live.

TeleVault runs one archiver (main.py) and, on demand, one backfill (backfill.py) PER ACCOUNT, as separate subprocesses spawned by the API server.
Those processes and the API server never talk to each other directly;
they coordinate through two small files per account:

    <data_dir>/users/<user_id>/televault.heartbeat    written every few seconds by main.py while it is connected
    <data_dir>/users/<user_id>/backfill_status.json   written by backfill.py as it progresses

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
