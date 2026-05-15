"""
Small utilities shared across handler modules.
 
Kept here rather than in utils/ because these are tightly coupled to
Telethon types and have no use outside the handlers package.
"""

import logging
from telethon import types

logger = logging.getLogger(__name__)


def get_chat_type(chat: object) -> str:
    """
    Map a Telethon chat entity to one of the four strings our schema accepts.
 
    Telethon represents chats as three distinct classes:
      - User    -> a direct / private conversation
      - Chat    -> a legacy group (up to 200 members, non-supergroup)
      - Channel -> both supergroups and broadcast channels share this class;
                  the megagroup flag distinguishes them
 
    The fallback logs a warning and returns 'group' so the INSERT doesn't
    violate the CHECK constraint while still surfacing unexpected types.
    """
    if isinstance(chat, types.User):
        return "private"
    if isinstance(chat, types.Chat):
        return "group"
    if isinstance(chat, types.Channel):
        return "supergroup" if chat.megagroup else "channel"
    
    logger.warning(f"Unknown chat entity type: {type(chat).__name__!r} — defaulting to 'group'.")

    return "group"


def get_sender_fields(sender: object | None) -> tuple[str | None, str | None, str | None]:
    """
    Extract (username, first_name, last_name) from a Telethon sender entity.
 
    Returns a triple of Nones when sender is None (e.g. broadcast channel
    posts, which have no individual author).
    """
    if sender is None:
        return None, None, None
    
    username = getattr(sender, "username", None)
    first_name = getattr(sender, "first_name", None)
    last_name = getattr(sender, "last_name", None)
    
    return username, first_name, last_name