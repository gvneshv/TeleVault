"""add message_media

Revision ID: c8a4e1f7b265
Revises: b6f2a8d1c394
Create Date: 2026-11-19 09:00:00.000000

Adds `message_media`: one row per message that carries a file TeleVault stores (photo, video, voice, audio, document),
with a status that says what became of the file (pending, stored, too_large, disk_full, unavailable, failed).
Nothing reads or writes it yet in this revision's release step;
the live download, backfill and file removal are built on top of it in later steps of Phase 4 (CHANGELOG.md, "Planned - Phase 4").

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives").
Archives provisioned after this change get the table automatically (db/provisioning.py runs the whole chain).

The CHECK constraints repeat db/schema.py's; a test compares the two so they cannot drift apart.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a4e1f7b265'
down_revision: Union[str, Sequence[str], None] = 'b6f2a8d1c394'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'message_media',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('mime_type', sa.Text(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('file_path', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint("kind IN ('photo', 'video', 'voice', 'audio', 'document')", name='ck_message_media_kind'),
        sa.CheckConstraint(
            "status IN ('pending', 'stored', 'too_large', 'disk_full', 'unavailable', 'failed')",
            name='ck_message_media_status',
        ),
        sa.CheckConstraint('size_bytes IS NULL OR size_bytes >= 0', name='ck_message_media_size'),
        sa.CheckConstraint("(status = 'stored') = (file_path IS NOT NULL)", name='ck_message_media_path'),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id'),
    )
    op.create_index('idx_message_media_status', 'message_media', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the table and with it every record of what was downloaded.
    FILES already on disk are NOT removed by a migration (it only touches the database);
    they become orphans for the orphan cleanup to find, or can be deleted by hand.
    """
    op.drop_index('idx_message_media_status', table_name='message_media')
    op.drop_table('message_media')