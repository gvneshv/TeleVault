"""
Endpoints for checking, starting, and stopping the CALLING account's live userbot (main.py) -
the process that holds that account's Telegram session and archives its messages in real time.

Every account has its own archiver: `python main.py --user-id <id>` is started per account, and everything here acts only on the caller's own.
(An earlier single-user design had exactly one archiver for the whole instance and gated these routes to its one owner - that is gone.)

main.py always runs as its OWN subprocess, never imported into this process,
same rationale as backfill.py (see api/routes/backfill.py's module docstring):
a crash/hang there must never affect the API server, and the two must not fight over the same Telegram session.

Liveness and control both go through the account's heartbeat file (utils/worker_paths.py), not an in-memory process handle:
main.py writes its own PID there on startup and deletes the file on clean shutdown (see main.py's heartbeat loop and shutdown section).
Reading the PID from that file - rather than keeping a Popen reference here - means /stop works correctly even if main.py was started outside this API
(cron, systemd, a manual `python main.py --user-id N`) or if the API server itself has restarted since main.py was started.
The one edge case this doesn't cover:
if the heartbeat has already gone stale (main.py died without cleaning up) AND the OS has since reused that PID for an unrelated process,
/stop would signal the wrong process.
Rare on a small private server, but worth knowing about.

What /start does NOT tell you: whether the worker then actually managed to log in.
The process is launched and this returns immediately;
if the saved Telegram session turns out to be invalid, main.py logs why and exits, and the status simply never becomes "running".
See main.py's "Telethon client" step.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_current_user, require_archiving_ready
from api.process_utils import pid_alive, is_backfill_running
from utils.security import DecodedAccessToken
from utils.worker_paths import backfill_status_path, heartbeat_path

router = APIRouter(prefix="/telethon", tags=["telethon"])
STALE_AFTER_SECONDS = 60

# main.py's location, absolute: the API server may have been started from any directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _heartbeat(user_id: int) -> dict | None:
    path = heartbeat_path(user_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _is_running(user_id: int) -> bool:
    data = _heartbeat(user_id)
    if data is None:
        return False
    return (time.time() - data["updated_at"]) < STALE_AFTER_SECONDS


@router.get("/status")
def get_telethon_status(current: DecodedAccessToken = Depends(get_current_user)):
    data = _heartbeat(current["user_id"])
    if data is None:
        return {"running": False}
    age = time.time() - data["updated_at"]
    return {"running": age < STALE_AFTER_SECONDS, "last_heartbeat_age_seconds": round(age, 1)}


@router.post("/start")
def start_archiver(current: DecodedAccessToken = Depends(require_archiving_ready)):
    user_id = current["user_id"]
    if _is_running(user_id):
        raise HTTPException(
            409, {"message": "The userbot is already running.", "reason": "already_running"}
        )
    if is_backfill_running(backfill_status_path(user_id)):
        raise HTTPException(
            409,
            {
                "message": "A backfill is currently running. Stop it before starting the "
                "userbot - Telethon sessions only support one active connection "
                "at a time.",
                "reason": "backfill_running",
            },
        )
    # Only the id goes on the command line - the Telegram session itself is read from the control DB by the worker (see utils/account.py).
    subprocess.Popen(
        [sys.executable, str(_REPO_ROOT / "main.py"), "--user-id", str(user_id)],
        cwd=_REPO_ROOT,
    )
    return {"started": True}


@router.post("/stop")
def stop_archiver(current: DecodedAccessToken = Depends(get_current_user)):
    user_id = current["user_id"]
    data = _heartbeat(user_id)
    if data is None or "pid" not in data or not pid_alive(data["pid"]):
        raise HTTPException(
            409, {"message": "The userbot is not currently running.", "reason": "not_running"}
        )
    try:
        os.kill(data["pid"], signal.SIGTERM)
    except OSError:
        # Died between the pid_alive() check above and here - already gone, nothing left to stop.
        raise HTTPException(
            409, {"message": "The userbot is not currently running.", "reason": "not_running"}
        )
    # Delete the heartbeat ourselves rather than waiting for main.py's own shutdown code to do it - on Windows that code never runs (see this module's docstring),
    # which otherwise left the heartbeat's last real timestamp sitting there,
    # making every other "is the archiver running?" check
    # (e.g. the backfill start guard) take up to STALE_AFTER_SECONDS to notice the process was actually already gone.
    heartbeat_path(user_id).unlink(missing_ok=True)
    return {"stopping": True}
