"""add the message ignore rules

Revision ID: b94e2d7a1c63
Revises: a3f81c5d7e92
Create Date: 2026-11-09 06:00:00.000000

Adds `ingest_ignore_rules` (text phrases that keep a message out of the archive) and `ingest_ignore_rule_chats` (the chats a rule is limited to).
The workers (handlers/on_message.py, handlers/on_edit.py, backfill.py) read them before a message is stored - see handlers/ingest_filter.py.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives").
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the workers treat missing tables as "no rules" and archive as before,
and the ignore-rule endpoints answer with a clear "archive needs updating" error (api/routes/ingest.py).

ingest_ignore_rule_chats has no foreign key to `chats` on purpose (a rule outlives its chat's data) - see db/schema.py's comment on the tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b94e2d7a1c63'
down_revision: Union[str, Sequence[str], None] = 'a3f81c5d7e92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ingest_ignore_rules',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('pattern', sa.Text(), nullable=False),
    sa.Column('pattern_key', sa.Text(), nullable=False),
    sa.Column('all_chats', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pattern_key')
    )
    op.create_table('ingest_ignore_rule_chats',
    sa.Column('rule_id', sa.BigInteger(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['rule_id'], ['ingest_ignore_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rule_id', 'chat_id')
    )
    op.create_index('idx_ignore_rule_chats_chat', 'ingest_ignore_rule_chats', ['chat_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops every ignore rule, so messages that matched one start being archived again - the only possible outcome of removing the feature's data.
    """
    op.drop_index('idx_ignore_rule_chats_chat', table_name='ingest_ignore_rule_chats')
    op.drop_table('ingest_ignore_rule_chats')
    op.drop_table('ingest_ignore_rules')
