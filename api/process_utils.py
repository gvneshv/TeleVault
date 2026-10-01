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


def running_process_reason(heartbeat_path, backfill_status_path) -> str | None:
    """
    Whether it's currently unsafe to unlink a Telegram session or delete an account,
    as a reason string a caller turns into its own error - or None if it's safe to proceed.

    Composes is_archiver_running()/is_backfill_running() above rather than either check alone:
    three call sites (api/routes/telegram.py's unlink(), api/routes/admin.py's delete_user(), and scripts/manage_admin.py's cmd_delete_admin())
    all need the same "is anything actively using Telegram or writing to an archive database right now" answer,
    for the same reason start_archiver()/start_backfill() (api/routes/telethon.py, api/routes/backfill.py) already treat the two as mutually exclusive:
    a live archiver holds the Telegram session open and writes to its archive continuously, and a running backfill writes to one too.
    Pulling a session out from under either, or dropping the database either is actively writing to, is exactly the kind of corruption/crash this exists to prevent -
    not something each of the three call sites should have to reason about (or risk forgetting to check) independently.

    Deliberately does NOT try to first work out whether the archiver/backfill in question actually belongs to the SAME account being unlinked/deleted
    (today, only ever the instance owner's - see api/dependencies.py's require_instance_owner() docstring for that concept) before blocking.
    There is exactly one physical Telethon process per instance regardless of which account this call is about,
    so a narrower "only block if it's THIS account's own session" check would need to duplicate that concept here for no real safety benefit today,
    and would need revisiting anyway the moment per-account worker processes (Decisions Log, still unbuilt) exist -
    simplest to hold every account to the same "nothing is actively running, instance-wide" bar now and revisit only if/when that assumption stops being true.

    Reuses the SAME reason strings start_archiver()'s own guards already use ("already_running", "backfill_running") rather than minting new ones -
    both are already wired end-to-end (web/js/lib/errors.js, i18n/{en,uk}.js), and the situation being reported is identical either way:
    something is running and has to be stopped first.
    """
    if is_archiver_running(heartbeat_path):
        return "already_running"
    if is_backfill_running(backfill_status_path):
        return "backfill_running"
    return None