"""add an index on messages (chat_id, tg_message_id)

Revision ID: d7b3f9a2e154
Revises: c5e9a1d3f742
Create Date: 2026-11-11 10:00:00.000000

The chat list now shows an unread badge per chat: "how many messages of this chat have a Telegram id above the owner's read position".
That is a range lookup on (chat_id, tg_message_id).
The only index that mentions tg_message_id, idx_messages_tg_id_chat, has it FIRST (it exists to keep ids unique per chat), so for a chat with small ids - every channel,
which numbers its messages on its own - Postgres would walk the entries of ALL chats above that number.
This index leads with chat_id and turns the lookup into a short range scan.

This is a per-account ARCHIVE database migration (alembic/, not alembic_control/); nothing applies it to archives that already exist:
run `python scripts/migrate_archives.py` (RUNBOOK.md, "Updating existing archives").
Archives provisioned later get it automatically.
Until an old archive is migrated nothing breaks, the badge query just reads more rows than it should.

A plain CREATE INDEX takes a lock that blocks writes to `messages` while it builds, which is a matter of seconds even for a few hundred thousand messages.
Run the migration with the archiver stopped, as the runbook already advises for every migration.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd7b3f9a2e154'
down_revision: Union[str, Sequence[str], None] = 'c5e9a1d3f742'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('idx_messages_chat_tg_id', 'messages', ['chat_id', 'tg_message_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_messages_chat_tg_id', table_name='messages')
