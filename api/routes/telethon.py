"""
Endpoints for checking, starting, and stopping the live userbot (main.py) - the process that holds the Telegram session and archives messages in real time.

main.py always runs as its OWN subprocess, never imported into this process,
same rationale as backfill.py (see api/routes/backfill.py's module docstring):
a crash/hang there must never affect the API server, and the two must not fight over the same Telegram session.

Liveness and control both go through the heartbeat file (settings.heartbeat_path), not an in-memory process handle:
main.py writes its own PID there on startup and deletes the file on clean shutdown (see main.py's heartbeat loop and shutdown section).
Reading the PID from that file - rather than keeping a Popen reference here - means /stop works correctly even if main.py was started outside this API
(cron, systemd, a manual `python main.py`) or if the API server itself has restarted since main.py was started.
The one edge case this doesn't cover:
if the heartbeat has already gone stale (main.py died without cleaning up) AND the OS has since reused that PID for an unrelated process,
/stop would signal the wrong process.
Rare on a personal single-user box, but worth knowing about.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import settings
from api.process_utils import pid_alive

router = APIRouter(prefix="/telethon", tags=["telethon"])
STALE_AFTER_SECONDS = 60


def _heartbeat() -> dict | None:
    path = Path(settings.heartbeat_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _is_running() -> bool:
    data = _heartbeat()
    if data is None:
        return False
    return (time.time() - data["updated_at"]) < STALE_AFTER_SECONDS


def _backfill_is_running() -> bool:
    path = Path(settings.backfill_status_path)
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text()).get("state") == "running"
    except Exception:
        return False


@router.get("/status")
def get_telethon_status():
    data = _heartbeat()
    if data is None:
        return {"running": False}
    age = time.time() - data["updated_at"]
    return {"running": age < STALE_AFTER_SECONDS, "last_heartbeat_age_seconds": round(age, 1)}


@router.post("/start")
def start_archiver():
    if _is_running():
        raise HTTPException(
            409, {"message": "The userbot is already running.", "reason": "already_running"}
        )
    if _backfill_is_running():
        raise HTTPException(
            409,
            {
                "message": "A backfill is currently running. Stop it before starting the "
                "userbot - Telethon sessions only support one active connection "
                "at a time.",
                "reason": "backfill_running",
            },
        )
    subprocess.Popen([sys.executable, "main.py"])
    return {"started": True}


@router.post("/stop")
def stop_archiver():
    data = _heartbeat()
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
    return {"stopping": True}