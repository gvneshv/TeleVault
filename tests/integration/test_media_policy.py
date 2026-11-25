"""
The file-type policy (migration e2b7c4d9a613): which of photo / video / voice / audio / document are stored.

An empty media_skipped_kinds table means "store everything".
A kind that is off still gets its message_media row (so the chat view can say "Photo"), but the row is 'skipped' and the downloader never sees it.
Saving the policy re-sorts the files already waiting, in one transaction.
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
from db.schema import media_skipped_kinds
from tests.conftest import REPO_ROOT
from tests.helpers import T0

REVISION = "e2b7c4d9a613"
PREVIOUS_REVISION = "c8a4e1f7b265"
ALL_ON = {"photo": True, "video": True, "voice": True, "audio": True, "document": True}


def message_with(conn, tg_id, kind):
    q.upsert_chat(conn, chat_id=11, name="c", chat_type="private")
    q.insert_message(conn, tg_message_id=tg_id, chat_id=11, sender_id=None, text=None, date=T0, media_kind=kind, media_size=10)


def statuses(conn):
    conn.rollback()
    return dict(conn.execute(sa.text("SELECT kind, status FROM message_media ORDER BY id")).all())


def set_status(conn, kind, status, path=None):
    conn.execute(sa.text("UPDATE message_media SET status = :s, file_path = :p WHERE kind = :k"), {"s": status, "p": path, "k": kind})
    conn.commit()


class TestPolicy:
    def test_everything_is_stored_by_default(self, archive):
        assert q.get_media_policy(archive.conn) == ALL_ON

    def test_the_saved_choice_is_read_back(self, archive):
        q.save_media_policy(archive.conn, ["photo", "document"])
        assert q.get_media_policy(archive.conn) == {"photo": True, "video": False, "voice": False, "audio": False, "document": True}

    def test_saving_replaces_the_previous_choice(self, archive):
        q.save_media_policy(archive.conn, ["photo"])
        q.save_media_policy(archive.conn, ["video", "voice", "audio", "document"])
        assert q.get_media_policy(archive.conn) == {**ALL_ON, "photo": False}

    def test_an_unknown_kind_is_refused_and_nothing_changes(self, archive):
        q.save_media_policy(archive.conn, ["photo"])
        with pytest.raises(ValueError):
            q.save_media_policy(archive.conn, ["photo", "gif"])
        assert q.get_media_policy(archive.conn)["video"] is False

    def test_the_table_only_knows_the_five_kinds(self, archive):
        with pytest.raises(IntegrityError):
            archive.conn.execute(sa.text("INSERT INTO media_skipped_kinds (kind) VALUES ('gif')"))
        archive.conn.rollback()


class TestNewFiles:
    def test_a_kind_that_is_on_is_queued(self, archive):
        message_with(archive.conn, 1, "photo")
        assert statuses(archive.conn) == {"photo": "pending"}

    def test_a_kind_that_is_off_is_recorded_but_not_queued(self, archive):
        q.save_media_policy(archive.conn, ["video"])
        message_with(archive.conn, 1, "photo")
        message_with(archive.conn, 2, "video")
        assert statuses(archive.conn) == {"photo": "skipped", "video": "pending"}
        assert q.next_pending_media(archive.conn) is not None  # the video, never the photo

    def test_the_downloader_never_sees_a_skipped_file(self, archive):
        q.save_media_policy(archive.conn, [])
        message_with(archive.conn, 1, "photo")
        assert q.next_pending_media(archive.conn) is None

    def test_media_skip_records_a_file_as_skipped_even_when_its_kind_is_on(self, archive):
        q.upsert_chat(archive.conn, chat_id=11, name="c", chat_type="private")
        q.insert_message(archive.conn, tg_message_id=1, chat_id=11, sender_id=None, text="cap", date=T0, media_kind="photo", media_size=1, media_skip=True)
        assert statuses(archive.conn) == {"photo": "skipped"}

    def test_a_missing_row_added_later_follows_the_same_rules(self, archive):
        q.upsert_chat(archive.conn, chat_id=11, name="c", chat_type="private")
        for i in (1, 2, 3):
            q.insert_message(archive.conn, tg_message_id=i, chat_id=11, sender_id=None, text="t", date=T0)
        q.save_media_policy(archive.conn, ["video"])
        assert q.add_missing_media_row(archive.conn, 1, 11, "photo", 5) is True  # off
        assert q.add_missing_media_row(archive.conn, 2, 11, "video", 5) is True  # on
        assert q.add_missing_media_row(archive.conn, 3, 11, "video", 5, skip=True) is True  # asked for
        archive.conn.rollback()
        rows = archive.conn.execute(sa.text("SELECT m.tg_message_id, mm.status FROM message_media mm JOIN messages m ON m.id = mm.message_id ORDER BY 1")).all()
        assert [tuple(r) for r in rows] == [(1, "skipped"), (2, "pending"), (3, "skipped")]

    def test_an_archive_without_the_migration_queues_everything(self, archive, monkeypatch):
        """Nothing to consult and no 'skipped' status to write: the old behaviour, unchanged."""
        monkeypatch.setattr(q, "media_policy_ready", lambda conn: False)
        assert q._initial_media_status_sql(archive.conn) == "'pending'"


class TestSavingResortsTheQueue:
    def seed(self, conn):
        for i, kind in enumerate(["photo", "video", "photo", "voice"], start=1):
            message_with(conn, i, kind)

    def test_waiting_files_of_a_kind_that_is_switched_off_are_set_aside(self, archive):
        self.seed(archive.conn)
        assert q.save_media_policy(archive.conn, ["video", "voice", "audio", "document"]) == {"skipped": 2, "queued": 0}
        archive.conn.rollback()
        rows = archive.conn.execute(sa.text("SELECT kind, status FROM message_media ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("photo", "skipped"), ("video", "pending"), ("photo", "skipped"), ("voice", "pending")]

    def test_switching_a_kind_back_on_queues_what_was_held_back(self, archive):
        self.seed(archive.conn)
        q.save_media_policy(archive.conn, ["video", "voice"])
        assert q.save_media_policy(archive.conn, ["photo", "video", "voice"]) == {"skipped": 0, "queued": 2}
        assert set(statuses(archive.conn).values()) == {"pending"}

    def test_a_stored_file_is_never_touched_either_way(self, archive):
        self.seed(archive.conn)
        set_status(archive.conn, "voice", "stored", "users/1/media/4")
        q.save_media_policy(archive.conn, ["photo"])
        assert statuses(archive.conn)["voice"] == "stored"
        q.save_media_policy(archive.conn, ["photo", "voice"])
        assert statuses(archive.conn)["voice"] == "stored"

    def test_refused_files_are_not_resurrected_by_the_policy(self, archive):
        """'too_large' and the like answer a different question (size); only the owner's explicit retry moves them."""
        self.seed(archive.conn)
        set_status(archive.conn, "video", "too_large")
        q.save_media_policy(archive.conn, [])
        q.save_media_policy(archive.conn, ["video"])
        assert statuses(archive.conn)["video"] == "too_large"


class TestMigration:
    def test_this_revision_follows_the_previous_one(self):
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION

    def test_the_migration_and_the_model_have_the_same_rule(self, archive):
        model = {c.name: " ".join(str(c.sqltext).split()) for c in media_skipped_kinds.constraints if isinstance(c, CheckConstraint) and c.name}
        rows = archive.conn.execute(
            sa.text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'media_skipped_kinds'::regclass AND contype = 'c'")
        ).all()
        assert {n for n, _ in rows} == set(model)
        for name, definition in rows:
            for word in ("photo", "video", "voice", "audio", "document"):
                assert (f"'{word}'" in model[name]) == (f"'{word}'" in definition)

    def test_downgrade_turns_skipped_files_back_into_pending_and_upgrade_restores_the_schema(self, server_ready):
        from db.provisioning import provision_archive_database

        name = provision_archive_database(900_111).db_name
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"] = db.server_database_url(name).render_as_string(hide_password=False)
        eng = sa.create_engine(db.server_database_url(name), poolclass=sa.pool.NullPool)
        try:
            with eng.connect() as conn:
                q.upsert_chat(conn, chat_id=11, name="c", chat_type="private")
                q.insert_message(conn, tg_message_id=1, chat_id=11, sender_id=None, text=None, date=T0, media_kind="photo", media_skip=True)  # commits itself
            command.downgrade(cfg, PREVIOUS_REVISION)
            with eng.connect() as conn:
                assert conn.execute(sa.text("SELECT status FROM message_media")).scalar() == "pending"
                assert conn.execute(sa.text("SELECT to_regclass('public.media_skipped_kinds')")).scalar() is None
            command.upgrade(cfg, "head")
            with eng.connect() as conn:
                assert conn.execute(sa.text("SELECT to_regclass('public.media_skipped_kinds')")).scalar() is not None
                conn.execute(sa.text("SELECT media_walked_id FROM chats"))
        finally:
            del os.environ["TELEVAULT_ALEMBIC_URL_OVERRIDE"]
            eng.dispose()
