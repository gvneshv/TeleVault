"""add the ingestion-time chat blocklist

Revision ID: 0e528a688c4c
Revises: dd006ba3b06d
Create Date: 2026-10-23 22:00:00.000000

Adds `ingest_blocked_chats`: chats the account owner chose not to archive.
The workers (handlers/on_message.py, handlers/on_edit.py, backfill.py) read it before touching any message content - see handlers/ingest_filter.py.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives") - it migrates every archive with the app's own database credentials,
so there is no per-archive URL or password to look up.
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the workers treat a missing table as "no rules" and archive as before,
and the blocklist endpoints answer with a clear "archive needs updating" error (api/routes/ingest.py).

No foreign key to `chats` on purpose - see db/schema.py's comment on the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0e528a688c4c'
down_revision: Union[str, Sequence[str], None] = 'dd006ba3b06d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ingest_blocked_chats',
    sa.Column('chat_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('blocked_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('chat_id')
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the blocklist, which means every blocked chat starts being archived again - the only possible outcome of removing the feature's data.
    """
    op.drop_table('ingest_blocked_chats')
