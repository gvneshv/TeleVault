"""Small process-management helpers shared by the backfill and telethon (archiver) routes."""
import json
import os
import subprocess
import time
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """
    Cross-platform "is this PID still running" check.

    NOTE: os.kill(pid, 0) is the standard POSIX no-op existence check,
    but on Windows it is NOT a safe no-op - CPython maps it to TerminateProcess(handle, 0),
    which actually kills the target (with exit code 0) instead of just probing it.
    So on Windows we shell out to tasklist instead, which only reads process state.
    """
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by another user - still "alive" for our purposes.
        return True
    except Exception:
        return False


def is_backfill_running(status_path) -> bool:
    """
    Whether a backfill is genuinely still running, per the persisted status file.

    Deliberately does NOT just trust status["state"] == "running":
    if the process that was supposed to update this file died without cleaning up
    (a hard kill, a crash, or - on Windows - the SIGTERM-maps-to-TerminateProcess gap documented in api/routes/backfill.py),
    the file can be left stuck saying "running" forever with nothing left alive to correct it.
    Checking the persisted pid's actual liveness is what makes that self-healing instead of a permanently stuck flag
    - used by both start_backfill()'s own "already running" guard and start_archiver()'s mutual-exclusion check, so a stuck file can't block the archiver either.
    """
    path = Path(status_path)
    if not path.exists():
        return False
    try:
        status = json.loads(path.read_text())
    except Exception:
        return False
    if status.get("state") != "running":
        return False
    pid = status.get("pid")
    return bool(pid) and pid_alive(pid)


def is_archiver_running(heartbeat_path, stale_after_seconds: int = 60) -> bool:
    """
    Whether the live userbot (main.py) currently holds the Telegram session, per its heartbeat file.

    main.py rewrites this file every HEARTBEAT_INTERVAL_SECONDS (20s) while connected and deletes it on clean shutdown
    - so its mere presence isn't proof of anything still running (a hard kill or crash leaves it behind).
    Requiring it to be recently-written is what makes this self-healing:
    a stale heartbeat is treated the same as no heartbeat at all, so it can never permanently block a backfill or a second archiver start.

    stale_after_seconds defaults to 60 (three missed heartbeats) - matches the threshold api/routes/telethon.py's own status endpoint uses.
    """
    path = Path(heartbeat_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return (time.time() - data["updated_at"]) < stale_after_seconds
    except Exception:
        return False