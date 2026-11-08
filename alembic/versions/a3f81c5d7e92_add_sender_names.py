"""add sender name history and display-name overrides

Revision ID: a3f81c5d7e92
Revises: 7c1d4a9e2b35
Create Date: 2026-11-08 12:00:00.000000

Adds two tables for the "Display names + username history" feature (Roadmap Phase 3):

  sender_name_history  - one row each time TeleVault SEES a sender under a different username / first name / last name than before
                         (written by db/queries.py's upsert_sender()).
  sender_display_names - the owner's own friendly label for a sender (written by PUT /api/senders/{id}/display-name).

Both reference `senders` with ON DELETE CASCADE: purging a chat deletes the senders that have no message left (db/queries.py's purge_chat()),
and what was recorded ABOUT such a person - their earlier names, the label the owner gave them - must go with them instead of lingering as orphans.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives"). No archiver restart is needed.
Archives provisioned after this change get the tables automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the archiver simply records no name history,
and the API answers "no overrides" for reads and 409 "archive_outdated" for writes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f81c5d7e92'
down_revision: Union[str, Sequence[str], None] = '7c1d4a9e2b35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('sender_name_history',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('sender_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.Text(), nullable=True),
    sa.Column('first_name', sa.Text(), nullable=True),
    sa.Column('last_name', sa.Text(), nullable=True),
    sa.Column('seen_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['sender_id'], ['senders.sender_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_sender_name_history_sender', 'sender_name_history', ['sender_id', 'id'], unique=False)
    op.create_table('sender_display_names',
    sa.Column('sender_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['sender_id'], ['senders.sender_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sender_id')
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the recorded name history and every display-name override - both are data the owner cannot get back
    (the history only exists because TeleVault watched it happen).
    The archiver resumes recording once the tables exist again.
    """
    op.drop_table('sender_display_names')
    op.drop_index('idx_sender_name_history_sender', table_name='sender_name_history')
    op.drop_table('sender_name_history')
