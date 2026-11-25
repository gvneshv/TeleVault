"""add media_skipped_kinds, chats.media_walked_id and the 'skipped' media status

Revision ID: e2b7c4d9a613
Revises: c8a4e1f7b265
Create Date: 2026-11-24 09:00:00.000000

Three small additions that belong together (Phase 4, media backfill modes):

* `media_skipped_kinds` - which file types the owner switched OFF (Settings -> Files).
  One row per kind; empty = store everything.
* `message_media.status` may now be 'skipped': the file is recorded but deliberately not downloaded (type switched off, or a "text only" backfill).
* `chats.media_walked_id` - how far a walk that also looked at FILES has covered a chat, so a media backfill is incremental like a text one.
  Separate from `backfill_covered_id`: a text-only walk passes messages whose files it ignored on purpose.
  The column starts empty for every chat, so the first "all" or "media" backfill of each chat after migrating walks it in full -
  that is what finds the files of messages archived before media archiving existed.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/), and nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` once (RUNBOOK.md, "Updating existing archives").
Until then everything keeps working: backfill modes other than "all" refuse to start, and files are all stored as before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7c4d9a613'
down_revision: Union[str, Sequence[str], None] = 'c8a4e1f7b265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_STATUSES = "status IN ('pending', 'stored', 'too_large', 'disk_full', 'unavailable', 'failed')"
_NEW_STATUSES = "status IN ('pending', 'stored', 'too_large', 'disk_full', 'unavailable', 'failed', 'skipped')"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chats', sa.Column('media_walked_id', sa.BigInteger(), nullable=True))
    op.drop_constraint('ck_message_media_status', 'message_media', type_='check')
    op.create_check_constraint('ck_message_media_status', 'message_media', _NEW_STATUSES)
    op.create_table(
        'media_skipped_kinds',
        sa.Column('kind', sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('photo', 'video', 'voice', 'audio', 'document')", name='ck_media_skipped_kinds_kind'),
        sa.PrimaryKeyConstraint('kind'),
    )


def downgrade() -> None:
    """Downgrade schema.

    'skipped' rows are turned back into 'pending' first (the old constraint does not know the word), so after a downgrade the downloader would fetch them:
    the policy that withheld them no longer exists.
    """
    op.drop_table('media_skipped_kinds')
    op.execute("UPDATE message_media SET status = 'pending' WHERE status = 'skipped'")
    op.drop_constraint('ck_message_media_status', 'message_media', type_='check')
    op.create_check_constraint('ck_message_media_status', 'message_media', _OLD_STATUSES)
    op.drop_column('chats', 'media_walked_id')
