"""add terms acceptance record to users

Revision ID: b7d2e91f4a63
Revises: e7e73e114fac
Create Date: 2026-10-08 12:00:00.000000

Records WHEN an account accepted the Terms of Service / acknowledged the Privacy Policy,
and WHICH version of them it saw (see api/routes/auth.py's CURRENT_TERMS_VERSION).
Both columns are nullable on purpose: accounts that already existed before this migration, and admin accounts created from the shell (scripts/manage_admin.py create),
never went through the registration checkbox - NULL honestly says "no recorded acceptance" instead of backfilling a fake timestamp.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e91f4a63'
down_revision: Union[str, Sequence[str], None] = 'e7e73e114fac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('terms_accepted_at', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('users', sa.Column('terms_version', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'terms_version')
    op.drop_column('users', 'terms_accepted_at')