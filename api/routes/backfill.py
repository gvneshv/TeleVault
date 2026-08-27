"""
Endpoints for triggering, monitoring, and reviewing backfill runs from the web UI.

backfill.py always runs as its OWN subprocess with its own TelegramClient - never imported into this process - so a crash/hang there never affects the API server,
and the API never needs its own Telegram session.
Progress is shared only via backfill_status_path (JSON) and the backfill_runs table - no in-process coupling.

Liveness/cancellation is checked two ways, deliberately:
the in-memory _process handle (fast, and lets cancel() reap the child properly with .wait())
AND the pid persisted in the status file by backfill.py itself on startup.
The second one matters because _process is lost if the API server restarts
(e.g. --reload picking up an unrelated code change) while a backfill is running - without a durable PID to fall back on,
that backfill becomes unstoppable and its status file stays stuck on "running" forever, with nothing left able to correct it.

Windows note:
sending SIGTERM to another process (via Popen.send_signal/os.kill) maps to an immediate TerminateProcess() on Windows,
not a real signal - the target's own signal.signal(SIGTERM, ...) handler in backfill.py never runs,
so it never gets the chance to write its own final "cancelled" status.
That's why cancel_backfill() below writes the terminal state itself right after signaling,
instead of waiting for/trusting the child to report its own exit.
"""
import json
import os
import signal
import subprocess
import sys
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from config import settings

from api.dependencies import get_db
from api.process_utils import pid_alive

router = APIRouter(prefix="/backfill", tags=["backfill"])
STALE_AFTER_SECONDS = 60
_process: subprocess.Popen | None = None


class BackfillStartRequest(BaseModel):
    chat: str | None = None
    limit: int | None = None


def _telethon_is_running() -> bool:
    path = Path(settings.heartbeat_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return (time.time() - data["updated_at"]) < STALE_AFTER_SECONDS
    except Exception:
        return False


def _read_status() -> dict:
    path = Path(settings.backfill_status_path)
    if not path.exists():
        return {"state": "idle"}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"state": "idle"}


def _write_status(data: dict) -> None:
    path = Path(settings.backfill_status_path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _running_pid() -> int | None:
    """
    Returns a live PID for the current backfill run, checking both the in-memory handle and the persisted status file,
    or None if nothing is actually running
    (even if the status file's "state" still says otherwise - a stale "running" with a dead PID means it crashed or was hard-killed without cleaning up after itself).
    """
    if _process is not None and _process.poll() is None:
        return _process.pid
    status = _read_status()
    if status.get("state") == "running":
        pid = status.get("pid")
        if pid and pid_alive(pid):
            return pid
    return None


@router.post("/start")
def start_backfill(body: BackfillStartRequest):
    global _process
    if _running_pid() is not None:
        raise HTTPException(
            409,
            {"message": "A backfill is already running.", "reason": "already_running"},
        )
    if _telethon_is_running():
        raise HTTPException(
            409,
            {
                "message": "The live userbot (main.py) appears to be connected. Stop it "
                "before starting a backfill - Telethon sessions only support one "
                "active connection at a time.",
                "reason": "archiver_connected",
            },
        )
    cmd = [sys.executable, "backfill.py"]
    if body.chat:
        cmd += ["--chat", body.chat]
    if body.limit:
        cmd += ["--limit", str(body.limit)]
    _process = subprocess.Popen(cmd)
    return {"started": True, "pid": _process.pid}


@router.post("/cancel")
def cancel_backfill():
    global _process
    pid = _running_pid()
    if pid is None:
        raise HTTPException(
            409, {"message": "No backfill is currently running.", "reason": "not_running"}
        )

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or we can't signal it - either way there's nothing left to terminate.
        # Fall through and still correct the status file below, since a dead process obviously isn't "running" regardless.
        pass

    if _process is not None:
        # Reap the child if it's the one we started this session, so it doesn't linger as a zombie.
        # Short timeout: on Windows the kill above was already an immediate TerminateProcess, so this returns fast;
        # on POSIX the graceful handler may take a moment to finish the current message before exiting.
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _process = None

    # Write the terminal state ourselves rather than trusting the child to - see this file's module docstring for why
    # (Windows bypasses its graceful-shutdown code entirely).
    status = _read_status()
    status["state"] = "cancelled"
    _write_status(status)

    return {"cancelling": True}


@router.get("/status")
def get_backfill_status():
    return _read_status()


@router.get("/history")
def get_backfill_history(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        "SELECT id, started_at, finished_at, status, chats_total, chats_done, "
        "messages_stored, messages_skipped, error_message FROM backfill_runs "
        "ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    return [dict(row) for row in rows]