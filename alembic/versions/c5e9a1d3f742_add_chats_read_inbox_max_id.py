"""add the owner's Telegram read position to chats

Revision ID: c5e9a1d3f742
Revises: b94e2d7a1c63
Create Date: 2026-11-10 09:00:00.000000

Adds `chats.read_inbox_max_id`: the id of the newest message of the chat that the account owner has read IN TELEGRAM (Telegram's own `read_inbox_max_id`).
The archiver mirrors it (handlers/on_read.py) and the chat view draws an "Unread messages" bar after it.
NULL means "not known yet": the chat view then draws no bar.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives"), then restart the archiver so it fills the column.
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the archiver logs one warning and skips read tracking, and the API answers `null`.

A plain nullable column with no default: adding it rewrites nothing, so it is instant however many messages the archive holds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e9a1d3f742'
down_revision: Union[str, Sequence[str], None] = 'b94e2d7a1c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chats', sa.Column('read_inbox_max_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Forgets where the owner had read up to; the archiver fills it again from Telegram on its next start.
    """
    op.drop_column('chats', 'read_inbox_max_id')
