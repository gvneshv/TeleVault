"""add chats.backfill_covered_id

Revision ID: b6f2a8d1c394
Revises: a9d2e5c7b814
Create Date: 2026-11-17 09:00:00.000000

Adds `chats.backfill_covered_id`: how far a backfill has walked a chat's history (every message id up to it was fetched and judged).
backfill.py passes it to Telegram as min_id, so a repeat run only asks for what is newer.

Why a new column and not "the highest message id we hold": the live archiver stores messages too.
A chat first seen live at message 1000 would look covered up to 1000 although messages 1-999 were never fetched, and an incremental backfill would skip them for good.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives").
Archives provisioned after this change get the column automatically (db/provisioning.py runs the whole chain).
Until an old archive is migrated nothing breaks: every backfill simply walks each chat's whole history, as a first run does, and logs one warning.
After migrating, the FIRST backfill of each chat is a full walk too (the column starts empty for every chat) and later ones are incremental.
That is intentional: no existing row can honestly be called "covered" - earlier runs recorded nothing per chat.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6f2a8d1c394'
down_revision: Union[str, Sequence[str], None] = 'a9d2e5c7b814'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chats', sa.Column('backfill_covered_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Forgets how far each chat was backfilled: the next backfill walks every chat in full again (safe - stored messages are skipped, never duplicated).
    """
    op.drop_column('chats', 'backfill_covered_id')
