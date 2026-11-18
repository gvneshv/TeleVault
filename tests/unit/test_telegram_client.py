"""utils/telegram_client.py - the identity and flood-wait settings every Telethon client TeleVault creates carries."""

import ast
from pathlib import Path

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings
from utils import telegram_client
from utils.telegram_client import FLOOD_SLEEP_THRESHOLD, client_options

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_the_options_are_the_configured_identity_plus_the_flood_threshold():
    assert client_options() == {
        "device_model": settings.telegram_device_model,
        "system_version": settings.telegram_system_version,
        "app_version": settings.telegram_app_version,
        "flood_sleep_threshold": 60,
    }
    assert FLOOD_SLEEP_THRESHOLD == 60


def test_settings_are_read_on_every_call_not_frozen_at_import(monkeypatch):
    monkeypatch.setattr(telegram_client, "settings", type("S", (), {
        "telegram_device_model": "Box", "telegram_system_version": "OS 1", "telegram_app_version": "9.9"})())
    assert client_options()["device_model"] == "Box"


def test_telethon_really_sends_these_strings_instead_of_its_own_defaults():
    """What Telegram's Devices list shows comes from the InitConnection request the client builds - check that one, on a real (unconnected) client."""
    client = TelegramClient(StringSession(), 1, "x" * 32, **client_options())
    init = client._init_request
    assert init.device_model == settings.telegram_device_model
    assert init.system_version == settings.telegram_system_version
    assert init.app_version == settings.telegram_app_version
    assert client.flood_sleep_threshold == 60
    # The defaults this replaces: the library's version number as the app version, and an architecture / "Unknown" as the device.
    assert init.app_version != TelegramClient.__version__
    assert init.device_model.lower() not in ("unknown", "x86_64", "amd64", "pc 64bit")


def _telegram_client_calls():
    """Every `TelegramClient(...)` call in the application code (not tests, not virtualenvs), as (file, line, call node)."""
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in ("tests", "venv", ".venv", "node_modules", "alembic", "alembic_control"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "TelegramClient":
                yield rel.as_posix(), node.lineno, node


def test_every_client_the_application_builds_carries_the_identity_options():
    """A future call site that forgets `**client_options()` would show Telethon's defaults in the Devices list again and skip the explicit flood threshold."""
    calls = list(_telegram_client_calls())
    assert len(calls) >= 5, "expected main.py, backfill.py, utils/telegram_sessions.py and two login routes"
    for file, line, node in calls:
        spread = [k.value for k in node.keywords if k.arg is None]
        assert any(isinstance(v, ast.Call) and getattr(v.func, "id", None) == "client_options" for v in spread), f"{file}:{line}"
