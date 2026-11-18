"""
How every Telethon client TeleVault creates names itself to Telegram, and how patiently it handles Telegram's rate limits.

Why this exists
    At every connect Telethon sends Telegram three strings about itself, and Telegram shows them to the account owner under Settings -> Devices:
    device_model, system_version and app_version.
    Left alone, Telethon fills them with the CPU architecture ("x86_64"), the raw kernel release ("6.8.0-45-generic") and its own version number
    - which tells anyone reading the list "a script", and for a session TeleVault holds all day that is not what we want to show.
    So all five places that build a TelegramClient (main.py, backfill.py, utils/telegram_sessions.py, the send-code and QR login routes)
    spread client_options() into the constructor, and the same three values are sent no matter which of them connects.

What the values are
    Plain, truthful names, not an imitation of a real Telegram app (copying "Telegram Android 10.x" or "iPhone 15" would be a false statement about what is connecting):
      - device_model:    "TeleVault Server"                       (TELEGRAM_DEVICE_MODEL)
      - system_version:  the host OS, e.g. "Linux 6.8.0"          (TELEGRAM_SYSTEM_VERSION)
      - app_version:     TeleVault's own version, config.APP_VERSION (TELEGRAM_APP_VERSION)
    All three are overridable in .env (config.py).
    A blank override falls back to the default, so none of them can ever be empty or "Unknown".

    Known limit: a session that was created BEFORE this change keeps the device strings it was created with in Telegram's list.
    Whether a later connect refreshes them is not something we verified; linking the account again creates the session under the new values.

Flood waits
    Telegram answers "too many requests" with FloodWaitError(seconds).
    Telethon sleeps through short ones by itself and only raises when the wait is longer than flood_sleep_threshold.
    60 seconds is Telethon's own default; it is passed explicitly here so the behaviour is written down in one place and cannot change with a library upgrade.
    A wait above the threshold reaches our code as FloodWaitError and is handled where the request is made (see backfill.py).
"""

from typing import Any

from config import settings

# Telethon sleeps through any FloodWaitError up to this many seconds without raising.
# See "Flood waits" above.
FLOOD_SLEEP_THRESHOLD = 60


def client_options() -> dict[str, Any]:
    """
    Keyword arguments for every TelegramClient(...) TeleVault builds: its device identity and its flood-wait threshold.

    Read from `settings` on each call (not at import), so a test can change the settings and the next client sees them.
    """
    return {
        "device_model": settings.telegram_device_model,
        "system_version": settings.telegram_system_version,
        "app_version": settings.telegram_app_version,
        "flood_sleep_threshold": FLOOD_SLEEP_THRESHOLD,
    }
