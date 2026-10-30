"""
Public re-exports for all Pydantic response models.
 
Import from here rather than from individual submodules so callers are insulated from internal restructuring:
 
    from api.schemas import MessageOut, ChatSummary, StatsOut
"""

from .chat import ChatOut, ChatSummary, ChatOption, ChatPurgedOut
from .message import SenderOut, EditOut, DeletionOut, MessageOut, MessageDetail, ChatWindowOut
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
    TelegramQrStartOut,
    TelegramQrPollOut,
    TelegramStatusOut,
    TelegramUnlinkOut,
    TelegramDeviceOut,
    TelegramDevicesOut,
    TelegramDeviceEndedOut,
)
from .archive import ArchiveProvisionOut
from .ingest import BlockedChatOut, BlocklistOut, BlockedChatRemovedOut
from .admin import (
    AdminUserOut,
    AdminUserListOut,
    AdminActionOut,
    AdminUserDeleteOut,
    AdminInviteCreateIn,
    AdminInviteOut,
    AdminInviteListItemOut,
    AdminInviteListOut,
)

__all__ = [
    # chat
    "ChatOut",
    "ChatSummary",
    "ChatOption",
    "ChatPurgedOut",
    # message
    "SenderOut",
    "EditOut",
    "DeletionOut",
    "MessageOut",
    "MessageDetail",
    "ChatWindowOut",
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
    "TelegramQrStartOut",
    "TelegramQrPollOut",
    "TelegramConfirmIn",
    "TelegramConfirmOut",
    "TelegramStatusOut",
    "TelegramUnlinkOut",
    "TelegramDeviceOut",
    "TelegramDevicesOut",
    "TelegramDeviceEndedOut",
    # archive
    "ArchiveProvisionOut",
    # ingest blocklist
    "BlockedChatOut",
    "BlocklistOut",
    "BlockedChatRemovedOut",
    # admin
    "AdminUserOut",
    "AdminUserListOut",
    "AdminActionOut",
    "AdminUserDeleteOut",
    "AdminInviteCreateIn",
    "AdminInviteOut",
    "AdminInviteListItemOut",
    "AdminInviteListOut",
]