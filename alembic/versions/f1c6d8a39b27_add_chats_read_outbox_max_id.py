"""add how far the other person has read the owner's messages to chats

Revision ID: f1c6d8a39b27
Revises: e4a8c2b6d913
Create Date: 2026-11-13 09:00:00.000000

Adds `chats.read_outbox_max_id`: the id of the newest message of the chat that the OTHER person has read -
Telegram's own `read_outbox_max_id`, the "double check mark".
The archiver mirrors it for PRIVATE chats only (handlers/on_outbox_read.py and the start-up sync in handlers/on_read.py),
and the chat view draws one or two check marks on the owner's own messages from it.
NULL means "not known yet" (never reported, or not a private chat): the chat view then draws no check marks at all.

It is the counterpart of `read_inbox_max_id` (c5e9a1d3f742), which is how far the OWNER has read.
Only this one number is kept per chat: no read times, no history of positions.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives"), then restart the archiver so it fills the column.
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the archiver logs one warning and skips this, and the API answers `null`.

A plain nullable column with no default: adding it rewrites nothing, so it is instant however many messages the archive holds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1c6d8a39b27'
down_revision: Union[str, Sequence[str], None] = 'e4a8c2b6d913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chats', sa.Column('read_outbox_max_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Forgets how far the other people have read; the archiver fills it again from Telegram on its next start.
    """
    op.drop_column('chats', 'read_outbox_max_id')
