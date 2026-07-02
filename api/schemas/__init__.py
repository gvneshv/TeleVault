"""
Public re-exports for all Pydantic response models.
 
Import from here rather than from individual submodules so callers are insulated from internal restructuring:
 
    from api.schemas import MessageOut, ChatSummary, StatsOut
"""

from .chat import ChatOut, ChatSummary
from .message import SenderOut, EditOut, DeletionOut, MessageOut, MessageDetail
from .stats import ChatStatRow, StatsOut
from .common import PaginatedResponse, HealthOut
 
__all__ = [
    # chat
    "ChatOut",
    "ChatSummary",
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
]
