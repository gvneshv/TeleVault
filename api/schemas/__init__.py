"""
Public re-exports for all Pydantic response models.
 
Import from here rather than from individual submodules so callers are insulated from internal restructuring:
 
    from api.schemas import MessageOut, ChatSummary, StatsOut
"""

from .chat import ChatOut, ChatSummary, ChatOption
from .message import SenderOut, EditOut, DeletionOut, MessageOut, MessageDetail
from .stats import ChatStatRow, StatsOut
from .common import PaginatedResponse, HealthOut
from .auth import RegisterIn, LoginIn, AccessTokenOut, UserOut
from .telegram import (
    TelegramCredentialsIn,
    TelegramCredentialsOut,
    TelegramSendCodeIn,
    TelegramSendCodeOut,
    TelegramConfirmIn,
    TelegramConfirmOut,
)

__all__ = [
    # chat
    "ChatOut",
    "ChatSummary",
    "ChatOption",
    # message
    "SenderOut",
    "EditOut",
    "DeletionOut",
    "MessageOut",
    "MessageDetail",
    # stats
    "ChatStatRow",
    "StatsOut",
    # common
    "PaginatedResponse",
    "HealthOut",
    # auth
    "RegisterIn",
    "LoginIn",
    "AccessTokenOut",
    "UserOut",
    # telegram
    "TelegramCredentialsIn",
    "TelegramCredentialsOut",
    "TelegramSendCodeIn",
    "TelegramSendCodeOut",
    "TelegramConfirmIn",
    "TelegramConfirmOut",
]