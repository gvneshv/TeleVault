"""
control_db - TeleVault control-database package (accounts/invites/refresh tokens/audit log).

Re-exports the public interface so the rest of the application imports from one place rather than
knowing which submodule each symbol lives in - same convention as db/__init__.py:

    from control_db import init_control_db, queries
"""

from control_db.connection import init_control_db, close_db, get_connection
from control_db import queries

__all__ = ["init_control_db", "close_db", "get_connection", "queries"]