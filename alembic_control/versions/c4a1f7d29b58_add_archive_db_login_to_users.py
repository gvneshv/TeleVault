"""add each archive's own database login to users

Revision ID: c4a1f7d29b58
Revises: b7d2e91f4a63
Create Date: 2026-10-28 06:00:00.000000

Adds `users.archive_db_user` (the Postgres role created for the account's archive database) and `users.archive_db_password` (that role's password,
Fernet-encrypted by the application exactly like the telegram_* credential columns; this migration never sees or writes a password).

Both nullable on purpose: every archive that exists today was created with the server's shared login and has no role of its own yet.
NULL means "reach this archive with the server's own login", the behaviour before this migration, so applying it changes nothing by itself -
scripts/secure_archives.py is what gives existing archives their own role.
Archives provisioned after this change get one automatically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a1f7d29b58'
down_revision: Union[str, Sequence[str], None] = 'b7d2e91f4a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('archive_db_user', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('archive_db_password', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the stored logins. The roles themselves stay in Postgres and the archives keep working with the server's shared login
    (the application falls back to it when no login is stored), so nothing breaks - the isolation is simply gone until scripts/secure_archives.py is run again.
    """
    op.drop_column('users', 'archive_db_password')
    op.drop_column('users', 'archive_db_user')
