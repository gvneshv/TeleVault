"""add the retention policy (automatic deletion of old archived messages)

Revision ID: a9d2e5c7b814
Revises: f1c6d8a39b27
Create Date: 2026-11-14 12:00:00.000000

Adds `retention_policy`: ONE row (or none) holding the owner's setting "delete archived messages older than N months", plus what the last run did.
It is a setting, not archived content: it survives "Delete all stored data" like the blocklist and the ignore rules do.

The policy is OFF until the owner saves it, and saving needs a confirmed message count (db/queries.py, "Retention"; utils/retention_rules.py),
because deleting is irreversible. Nothing is deleted by this migration.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives"), then restart the archiver so it starts the job.
Archives provisioned after this change get it automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: the archiver logs one line and skips retention, and the API answers 409 `archive_outdated` for the retention routes.

A new table only: no existing row is rewritten, so it is instant however many messages the archive holds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d2e5c7b814'
down_revision: Union[str, Sequence[str], None] = 'f1c6d8a39b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'retention_policy',
        sa.Column('singleton', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('keep_deleted', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('confirmed_messages', sa.BigInteger(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_run_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('last_run_status', sa.Text(), nullable=True),
        sa.Column('last_run_deleted', sa.BigInteger(), nullable=True),
        sa.CheckConstraint('singleton', name='ck_retention_policy_singleton'),
        sa.CheckConstraint('months BETWEEN 1 AND 120', name='ck_retention_policy_months'),
        sa.CheckConstraint("last_run_status IN ('ok', 'blocked', 'error')", name='ck_retention_policy_last_run_status'),
        sa.PrimaryKeyConstraint('singleton'),
    )


def downgrade() -> None:
    """Downgrade schema.

    Forgets the policy (it is off again) - nothing archived is touched, and nothing already deleted comes back.
    """
    op.drop_table('retention_policy')
