"""add the owner's local "dismiss the unread bar" position to chats

Revision ID: e4a8c2b6d913
Revises: d7b3f9a2e154
Create Date: 2026-11-12 09:00:00.000000

Adds `chats.unread_dismissed_id`: the Telegram id up to which the owner pressed "Dismiss" on the chat view's "Unread messages" bar.
It exists because Telegram's own read position (`read_inbox_max_id`) can stay behind for good:
a message that was deleted before it was read may leave the position short of it,
and the bar and the chat list's badge then claim unread messages Telegram no longer counts.

The chat view treats a message as read when its id is up to the LARGER of the two numbers.
The new column is TeleVault's own bookkeeping: it is never sent to Telegram and marks nothing as read there.
NULL means "never dismissed" (the normal case).

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives").
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the bar has no "Dismiss" that works (the API answers 409 "archive_outdated") and everything else is unchanged.

A plain nullable column with no default: adding it rewrites nothing, so it is instant however many messages the archive holds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a8c2b6d913'
down_revision: Union[str, Sequence[str], None] = 'd7b3f9a2e154'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chats', sa.Column('unread_dismissed_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Forgets every "Dismiss": a bar the owner had dismissed comes back if Telegram's own position is still behind.
    Unlike the read position this is the owner's own choice, which nothing can refill.
    """
    op.drop_column('chats', 'unread_dismissed_id')
