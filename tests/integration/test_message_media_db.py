"""
The message_media table (migration c8a4e1f7b265): its rules, its cascade, and that the model and the migration say the same thing.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import IntegrityError

import db
import db.queries as q
from tests.conftest import REPO_ROOT
from tests.helpers import T0

REVISION = "c8a4e1f7b265"
PREVIOUS_REVISION = "b6f2a8d1c394"


def add_message(conn, tg_id=1):
    q.upsert_chat(conn, chat_id=11, name="c", chat_type="private")
    return q.insert_message(conn, tg_message_id=tg_id, chat_id=11, sender_id=None, text=None, date=T0)


def add_media(conn, message_id, **values):
    row = {"message_id": message_id, "kind": "photo", "status": "pending", **values}
    conn.execute(
        sa.text(
            "INSERT INTO message_media (message_id, kind, status, size_bytes, file_path) "
            "VALUES (:message_id, :kind, :status, :size_bytes, :file_path)"
        ),
        {"size_bytes": None, "file_path": None, **row},
    )


class TestRules:
    def test_a_default_row_is_pending(self, archive):
        mid = add_message(archive.conn)
        archive.conn.execute(sa.text("INSERT INTO message_media (message_id, kind) VALUES (:m, 'video')"), {"m": mid})
        assert archive.conn.execute(sa.text("SELECT status FROM message_media")).scalar() == "pending"

    @pytest.mark.parametrize(
        "values",
        [
            {"kind": "gif"},  # not a stored kind
            {"status": "done"},  # not a status
            {"size_bytes": -1},
            {"status": "stored", "file_path": None},  # stored means there is a file
            {"status": "too_large", "file_path": "users/1/x"},  # and only stored has one
        ],
    )
    def test_the_database_refuses_nonsense(self, archive, values):
        mid = add_message(archive.conn)
        with pytest.raises(IntegrityError):
            add_media(archive.conn, mid, **values)
        archive.conn.rollback()

    def test_a_stored_file_has_a_path(self, archive):
        mid = add_message(archive.conn)
        add_media(archive.conn, mid, status="stored", file_path="users/1/media/a")
        assert archive.conn.execute(sa.text("SELECT file_path FROM message_media")).scalar() == "users/1/media/a"

    def test_one_media_row_per_message(self, archive):
        mid = add_message(archive.conn)
        add_media(archive.conn, mid)
        with pytest.raises(IntegrityError):
            add_media(archive.conn, mid)
        archive.conn.rollback()

    def test_deleting_the_message_removes_its_media_row(self, archive):
        mid = add_message(archive.conn)
        add_media(archive.conn, mid)
        archive.conn.execute(sa.text("DELETE FROM messages WHERE id = :m"), {"m": mid})
        assert archive.conn.execute(sa.text("SELECT count(*) FROM message_media")).scalar() == 0


class TestMigration:
    def test_this_revision_follows_the_previous_one(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_the_migration_and_the_model_have_the_same_rules(self, archive):
        """The CHECKs are written twice (db/schema.py and the migration); the real, migrated table must carry the model's."""
        from db.schema import message_media

        model = {c.name: " ".join(str(c.sqltext).split()) for c in message_media.constraints if isinstance(c, CheckConstraint) and c.name}
        rows = archive.conn.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'message_media'::regclass AND contype = 'c'"
            )
        ).all()
        assert {n for n, _ in rows} == set(model)
        for name, definition in rows:
            # Postgres re-prints the expression (casts, parentheses), so compare the words that matter.
            for word in ("photo", "video", "voice", "audio", "document", "pending", "stored", "too_large", "disk_full", "unavailable", "failed", "skipped"):
                assert (f"'{word}'" in model[name]) == (f"'{word}'" in definition)

    def test_downgrade_and_upgrade_round_trip(self, server_ready):
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_110).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)

        def exists():
            with eng.connect() as conn:
                return conn.execute(sa.text("SELECT to_regclass('public.message_media')")).scalar() is not None

        try:
            assert exists()
            command.downgrade(cfg, PREVIOUS_REVISION)
            assert not exists()
            command.upgrade(cfg, "head")
            assert exists()
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()