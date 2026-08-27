"""Small process-management helpers shared by the backfill and telethon (archiver) routes."""
import os
import subprocess


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
