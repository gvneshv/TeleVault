"""
Endpoints for triggering, monitoring, and reviewing backfill runs from the web UI - always the CALLING account's own.

Every account has its own backfill process (`python backfill.py --user-id <id>`), status file and history (utils/worker_paths.py, the account's own archive database),
so one account backfilling never blocks or reveals anything about another's.

backfill.py always runs as its OWN subprocess with its own TelegramClient - never imported into this process - so a crash/hang there never affects the API server,
and the API never needs its own Telegram session.
Progress is shared only via the account's backfill status file (JSON) and the backfill_runs table in its archive - no in-process coupling.

Liveness/cancellation is checked two ways, deliberately:
the in-memory handle in _processes (fast, and lets cancel() reap the child properly with .wait())
AND the pid persisted in the status file by backfill.py itself on startup.
The second one matters because _processes is lost if the API server restarts
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
import time
from pathlib import Path

from math import ceil

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection

import control_db
import db

from api.dependencies import (
    archive_target_for_user,
    get_archive_connection,
    get_control_db,
    get_current_user,
    require_archiving_ready,
)
from api.process_utils import pid_alive
from db.read_queries import get_backfill_history as fetch_backfill_history
from utils.atomic_write import atomic_write_json
from utils.security import DecodedAccessToken
from utils.worker_paths import backfill_status_path, heartbeat_path

router = APIRouter(prefix="/backfill", tags=["backfill"])
STALE_AFTER_SECONDS = 60

# user_id -> the backfill subprocess THIS API process started for that account (only used to reap the child cleanly on cancel).
# Keyed per account: several accounts can backfill at the same time, each with its own process.
_processes: dict[int, subprocess.Popen] = {}

# backfill.py's location, absolute: the API server may have been started from any directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class BackfillStartRequest(BaseModel):
    chat: str | None = None
    limit: int | None = None


def _telethon_is_running(user_id: int) -> bool:
    path = heartbeat_path(user_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return (time.time() - data["updated_at"]) < STALE_AFTER_SECONDS
    except Exception:
        return False


def _read_status(user_id: int) -> dict:
    path = backfill_status_path(user_id)
    if not path.exists():
        return {"state": "idle"}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"state": "idle"}


def _write_status(user_id: int, data: dict) -> None:
    atomic_write_json(backfill_status_path(user_id), data)


def _running_pid(user_id: int) -> int | None:
    """
    Returns a live PID for this account's current backfill run, checking both the in-memory handle and the persisted status file, or None if nothing is actually running
    (even if the status file's "state" still says otherwise - a stale "running" with a dead PID means it crashed or was hard-killed without cleaning up after itself).
    """
    process = _processes.get(user_id)
    if process is not None and process.poll() is None:
        return process.pid
    status = _read_status(user_id)
    if status.get("state") == "running":
        pid = status.get("pid")
        if pid and pid_alive(pid):
            return pid
    return None


@router.post("/start")
def start_backfill(body: BackfillStartRequest, current: DecodedAccessToken = Depends(require_archiving_ready)):
    user_id = current["user_id"]
    if _running_pid(user_id) is not None:
        raise HTTPException(
            409,
            {"message": "A backfill is already running.", "reason": "already_running"},
        )
    if _telethon_is_running(user_id):
        raise HTTPException(
            409,
            {
                "message": "The live userbot (main.py) appears to be connected. Stop it "
                "before starting a backfill - Telethon sessions only support one "
                "active connection at a time.",
                "reason": "archiver_connected",
            },
        )
    # Only the id goes on the command line - the Telegram session itself is read from the control DB by the worker (see utils/account.py).
    cmd = [sys.executable, str(_REPO_ROOT / "backfill.py"), "--user-id", str(user_id)]
    if body.chat:
        cmd += ["--chat", body.chat]
    if body.limit:
        cmd += ["--limit", str(body.limit)]
    process = subprocess.Popen(cmd, cwd=_REPO_ROOT)
    _processes[user_id] = process
    return {"started": True, "pid": process.pid}


@router.post("/cancel")
def cancel_backfill(
    current: DecodedAccessToken = Depends(get_current_user),
    control_conn: Connection = Depends(get_control_db),
):
    user_id = current["user_id"]
    pid = _running_pid(user_id)
    if pid is None:
        raise HTTPException(
            409,
            {"message": "No backfill is currently running.", "reason": "not_running"},
        )

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or we can't signal it - either way there's nothing left to terminate.
        # Fall through and still correct the status file below, since a dead process obviously isn't "running" regardless.
        pass

    process = _processes.pop(user_id, None)
    if process is not None:
        # Reap the child if it's the one we started this session, so it doesn't linger as a zombie.
        # Short timeout: on Windows the kill above was already an immediate TerminateProcess, so this returns fast;
        # on POSIX the graceful handler may take a moment to finish the current message before exiting.
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # Write the terminal state ourselves rather than trusting the child to - see this file's module docstring for why
    # (Windows bypasses its graceful-shutdown code entirely).
    status = _read_status(user_id)
    status["state"] = "cancelled"
    _write_status(user_id, status)

    # Same reasoning applies to the backfill_runs history row:
    # it's normally only written once, right at the natural end of run() - which a hard-killed process never reaches,
    # leaving its History entry stuck on "running" forever too.
    # run_id was persisted into the status file at startup specifically so this endpoint could reach it.
    #
    # This is a genuine write to the account's archive, so it opens an explicit read-write tenant connection (db.get_tenant_connection())
    # rather than the read-only get_archive_connection() dependency the history route below uses
    # (that one is read-only - this UPDATE would be rejected by Postgres itself if run through it).
    run_id = status.get("run_id")
    if run_id is not None:
        user = control_db.queries.get_user_by_id(control_conn, user_id)
        if user is not None and user["archive_db_ref"] is not None:
            archive_db_ref, login = archive_target_for_user(user)
            with db.get_tenant_connection(archive_db_ref, login) as conn:
                conn.execute(
                    sql_text(
                        "UPDATE backfill_runs SET finished_at = CURRENT_TIMESTAMP, status = 'cancelled', "
                        "chats_total = :chats_total, chats_done = :chats_done WHERE id = :run_id AND status = 'running'"
                    ),
                    {
                        "chats_total": status.get("chats_total"),
                        "chats_done": status.get("chats_done"),
                        "run_id": run_id,
                    },
                )
                conn.commit()

    return {"cancelling": True}


@router.get("/status")
def get_backfill_status(current: DecodedAccessToken = Depends(get_current_user)):
    return _read_status(current["user_id"])


@router.get("/history")
def get_backfill_history(
    page: int = Query(1, ge=1, description="Page number (1-based)."),
    per_page: int = Query(20, ge=1, le=200, description="Results per page."),
    conn: Connection = Depends(get_archive_connection),
):
    result = fetch_backfill_history(conn, page=page, per_page=per_page)
    total = result["total"]
    return {
        "items": result["items"],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, ceil(total / per_page)),
    }
