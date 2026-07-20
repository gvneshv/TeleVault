"""Endpoint for checking whether the live userbot (main.py) currently holds the Telegram session."""
import json
import time
from pathlib import Path

from fastapi import APIRouter

from config import settings

router = APIRouter(prefix="/api/telethon", tags=["telethon"])
STALE_AFTER_SECONDS = 60


@router.get("/status")
def get_telethon_status():
    path = Path(settings.heartbeat_path)
    if not path.exists():
        return {"running": False}
    try:
        data = json.loads(path.read_text())
        age = time.time() - data["updated_at"]
        return {"running": age < STALE_AFTER_SECONDS, "last_heartbeat_age_seconds": round(age, 1)}
    except Exception:
        return {"running": False}