"""
Thin, type-safe wrappers around the few Telethon calls whose published type hints disagree with their documented behaviour.

Why this module exists:
    Telethon's synchronous-style methods (`disconnect()`, `run_until_disconnected()`)
    are written so the same call works both in plain scripts and inside a running event loop.
    Inside a running loop they return a coroutine that MUST be awaited; outside one they block and return None.
    Telethon ships no return annotation for them,
    so static checkers infer `Coroutine | None` and (correctly, from their point of view) refuse `await client.disconnect()`.
    At runtime TeleVault's workers always call these from inside `asyncio.run()`, so the awaitable branch is the one that fires.

    Likewise `get_me()` is annotated `User | InputPeerUser` even though `input_peer=False` (the default) can only ever give a `User`.

Centralising the workarounds here (instead of sprinkling `# type: ignore` through main.py and backfill.py)
means the reasoning is written down once, the call sites stay clean, and a future Telethon release that fixes its hints only needs this one file touched.
"""

import inspect
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import User


async def disconnect_client(client: TelegramClient) -> None:
    """
    Disconnect `client`, awaiting the result only if Telethon handed back something awaitable.

    Safe to call on an already-disconnected client (Telethon treats that as a no-op).
    """
    result: Any = client.disconnect()
    if inspect.isawaitable(result):
        await result


async def run_until_disconnected(client: TelegramClient) -> None:
    """
    Block until `client` disconnects (Ctrl-C / SIGTERM handling stays with the caller).

    Same awaitable-or-None reasoning as disconnect_client().
    """
    result: Any = client.run_until_disconnected()
    if inspect.isawaitable(result):
        await result


async def get_authenticated_user(client: TelegramClient) -> User:
    """
    Return the logged-in account as a `User`, or raise RuntimeError if Telegram answered with anything else.

    Call only after `is_user_authorized()` has returned True.
    A non-`User` answer here means the session is in a state this app has no business continuing from,
    so failing loudly beats carrying on with a half-valid identity (the worker's caller owns cleanup of the client and database connections).
    """
    me = await client.get_me()
    if not isinstance(me, User):
        raise RuntimeError(f"Expected Telegram to return the logged-in User, got {type(me).__name__}.")
    return me