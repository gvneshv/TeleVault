"""add the archive owner's Telegram id

Revision ID: 7c1d4a9e2b35
Revises: 0e528a688c4c
Create Date: 2026-11-05 12:00:00.000000

Adds `archive_owner`: a single row holding the Telegram user id of the account this archive belongs to.
The archiver records it at start-up (main.py); the chat view reads it (GET /api/archive/owner) to draw the owner's own messages on the right.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives"), then restart the archiver so it records the id.
Archives provisioned after this change get the table automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the API answers "owner unknown" and the chat view keeps every message on the left.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1d4a9e2b35'
down_revision: Union[str, Sequence[str], None] = '0e528a688c4c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('archive_owner',
    sa.Column('singleton', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('owner_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('recorded_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('singleton', name='ck_archive_owner_singleton'),
    sa.PrimaryKeyConstraint('singleton')
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the recorded owner id; the archiver writes it again the next time it starts (once the table exists).
    """
    op.drop_table('archive_owner')
