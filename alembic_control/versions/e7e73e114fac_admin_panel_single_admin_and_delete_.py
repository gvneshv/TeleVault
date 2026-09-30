"""admin panel: single-admin index + delete-user ON DELETE behaviors

Revision ID: e7e73e114fac
Revises: 3be4e8d18aa0
Create Date: 2026-09-29 00:00:00.000000

Three independent, additive changes, all needed for the new admin-panel feature
(control_db.queries.delete_user_completely / lock_user / unlock_user, api/routes/admin.py):

1. ix_users_single_admin - a partial unique index on users.is_admin WHERE is_admin = true.
   TeleVault moved to a single-admin model (Decisions Log) - this is the database itself refusing a second admin row,
   the same way users.username's own unique constraint refuses a duplicate username, rather than relying solely on application code to check first.

2. invites.used_by: FK changed to ON DELETE SET NULL.
3. auth_audit_log.user_id / auth_audit_log.actor_id: FK changed to ON DELETE SET NULL.
4. refresh_tokens.user_id: FK changed to ON DELETE CASCADE.
   Together, 2-4 are what make delete_user_completely()'s hard DELETE FROM users possible at all:
   without them, Postgres would refuse to delete a users row that anything still points at.
   Invites and audit-log rows are KEPT with their reference nulled out (they have their own historical value independent of whether the account still exists);
   refresh tokens are pure session artifacts of the account and are deleted along with it.
   See control_db/schema.py's own column comments for the fuller reasoning behind each choice.

Safe to run against a database with existing data: assumes at most one row currently has is_admin=true
(true for every instance that followed scripts/manage_admin.py's own single-`create`-then-optionally-`promote` bootstrapping - promote has since been removed,
see that script's module docstring).
If some prior instance genuinely has two or more admins already,
step 1 below will fail loudly with a Postgres uniqueness violation rather than silently picking one -
resolve that by hand (decide which account stays admin) before re-running this migration.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7e73e114fac'
down_revision: Union[str, Sequence[str], None] = '3be4e8d18aa0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_users_single_admin",
        "users",
        ["is_admin"],
        unique=True,
        postgresql_where="is_admin = true",
    )

    op.drop_constraint("invites_used_by_fkey", "invites", type_="foreignkey")
    op.create_foreign_key(
        "invites_used_by_fkey", "invites", "users", ["used_by"], ["id"], ondelete="SET NULL"
    )

    op.drop_constraint("auth_audit_log_user_id_fkey", "auth_audit_log", type_="foreignkey")
    op.create_foreign_key(
        "auth_audit_log_user_id_fkey", "auth_audit_log", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )
    op.drop_constraint("auth_audit_log_actor_id_fkey", "auth_audit_log", type_="foreignkey")
    op.create_foreign_key(
        "auth_audit_log_actor_id_fkey", "auth_audit_log", "users", ["actor_id"], ["id"], ondelete="SET NULL"
    )

    op.drop_constraint("refresh_tokens_user_id_fkey", "refresh_tokens", type_="foreignkey")
    op.create_foreign_key(
        "refresh_tokens_user_id_fkey", "refresh_tokens", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("refresh_tokens_user_id_fkey", "refresh_tokens", type_="foreignkey")
    op.create_foreign_key(
        "refresh_tokens_user_id_fkey", "refresh_tokens", "users", ["user_id"], ["id"]
    )

    op.drop_constraint("auth_audit_log_actor_id_fkey", "auth_audit_log", type_="foreignkey")
    op.create_foreign_key(
        "auth_audit_log_actor_id_fkey", "auth_audit_log", "users", ["actor_id"], ["id"]
    )
    op.drop_constraint("auth_audit_log_user_id_fkey", "auth_audit_log", type_="foreignkey")
    op.create_foreign_key(
        "auth_audit_log_user_id_fkey", "auth_audit_log", "users", ["user_id"], ["id"]
    )

    op.drop_constraint("invites_used_by_fkey", "invites", type_="foreignkey")
    op.create_foreign_key(
        "invites_used_by_fkey", "invites", "users", ["used_by"], ["id"]
    )

    op.drop_index("ix_users_single_admin", table_name="users")