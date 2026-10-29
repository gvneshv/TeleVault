"""
Every archive has its OWN Postgres login, and that login opens that archive and nothing else.

This is the guard against "one account's connection reads or writes another account's archive":
the application picks the database by name, and this file proves that even if it ever picked wrongly (or a password leaked), Postgres itself would refuse.
Covers provisioning (a new archive), securing an existing one (scripts/secure_archives.py), and the connection layer's handling of logins.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.pool import NullPool

import db
from db import connection
from config import settings
from control_db import queries as cq
from db import provisioning
from scripts import secure_archives
from tests.helpers import seed_archive_by_name, seed_chat
from utils.archive_login import archive_login_from_row


def connect_as(db_name: str, login: db.ArchiveLogin | None) -> sa.Engine:
    """An engine on `db_name` authenticating as `login` (None = the server's login).
    Not connected yet - the test decides when it must succeed or fail."""
    return sa.create_engine(db.server_database_url(db_name, login), poolclass=NullPool)


def scalar(db_name: str, login: db.ArchiveLogin | None, sql: str):
    engine = connect_as(db_name, login)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(sql)).scalar()
    finally:
        engine.dispose()


def make_legacy_archive(user_id: int) -> str:
    """An archive exactly as it was created BEFORE per-archive logins: made and migrated with the server's own login, no role of its own."""
    name = provisioning._tenant_database_name(user_id)
    provisioning._create_database_if_missing(name)
    provisioning._migrate_database(name)
    return name


@pytest.fixture
def two(server_ready):
    """Two freshly provisioned archives, A and B (each: database + own role)."""
    return provisioning.provision_archive_database(900_030), provisioning.provision_archive_database(900_031)


class TestProvisionedArchiveLogin:
    def test_the_role_opens_its_own_archive_and_can_use_it(self, two):
        a, _ = two
        assert scalar(a.db_name, a.login, "SELECT current_user") == a.login.role
        assert scalar(a.db_name, a.login, "SELECT to_regclass('public.messages') IS NOT NULL") is True

    def test_each_archive_gets_its_own_role_and_password(self, two):
        a, b = two
        assert a.login.role != b.login.role
        assert a.login.password != b.login.password
        assert len(a.login.password) >= 32  # token_urlsafe(32): ~43 characters of randomness

    def test_one_archives_role_cannot_even_connect_to_another(self, two):
        """The point of the whole feature.
        Without REVOKE CONNECT ... FROM PUBLIC, Postgres lets every role into every database."""
        a, b = two
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(b.db_name, a.login).connect()
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(a.db_name, b.login).connect()

    def test_the_other_archives_data_is_unreachable_even_with_a_valid_login_for_the_wrong_database(self, two):
        a, b = two
        seed_archive_by_name(b.db_name, chat_id=1, name="B's secret chat", messages=[(1, 1, "B's secret message")])
        engine = connect_as(a.db_name, a.login)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM messages")).scalar() == 0  # A sees A's (empty) archive, never B's
        engine.dispose()

    def test_an_archive_role_cannot_connect_to_the_control_database(self, two):
        """The control database holds password hashes and Telegram sessions."""
        a, _ = two
        control_name = sa.make_url(settings.control_database_url).database
        assert control_name is not None
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(control_name, a.login).connect()

    def test_the_role_has_no_power_beyond_its_own_database(self, two):
        a, _ = two
        row = scalar_row(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r", r=a.login.role
        )
        assert tuple(row) == (False, False, False, False, False, True)

    def test_the_role_cannot_create_roles_or_databases(self, two):
        a, _ = two
        engine = connect_as(a.db_name, a.login).execution_options(isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            with pytest.raises(ProgrammingError):
                conn.execute(sa.text('CREATE ROLE "televault_test_intruder"'))
            with pytest.raises(ProgrammingError):
                conn.execute(sa.text('CREATE DATABASE "televault_test_intruder_db"'))
        engine.dispose()

    def test_only_the_owner_has_connect_on_the_archive(self, two):
        a, b = two
        priv = "SELECT has_database_privilege(:r, :d, 'CONNECT')"
        assert scalar_param(priv, r=a.login.role, d=a.db_name) is True
        assert scalar_param(priv, r=a.login.role, d=b.db_name) is False
        assert scalar_param(priv, r=b.login.role, d=a.db_name) is False

    def test_the_password_never_appears_when_the_login_is_printed(self, two):
        a, _ = two
        assert a.login.password not in repr(a.login)
        assert a.login.password not in repr(a)
        assert a.login.password not in str(db.server_database_url(a.db_name, a.login))  # SQLAlchemy masks it as ***


def scalar_row(sql: str, **params):
    engine = sa.create_engine(settings.control_database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(sql), params).one()
    finally:
        engine.dispose()


def scalar_param(sql: str, **params):
    return scalar_row(sql, **params)[0]


class TestSecuringAnExistingArchive:
    def test_data_survives_and_the_role_can_use_it(self, server_ready):
        name = make_legacy_archive(900_040)
        seed_archive_by_name(name, chat_id=7, name="Old chat", messages=[(1, 5, "written before securing")])
        login = provisioning.secure_archive_database(name)
        assert scalar(name, login, "SELECT text FROM messages") == "written before securing"
        assert scalar(name, login, "SELECT current_user") == login.role

    def test_the_role_can_write_and_migrate_after_it(self, server_ready):
        """Ownership of tables AND of the trigger functions moved, so the role can run the real migration chain (and the counters trigger still fires)."""
        name = make_legacy_archive(900_041)
        login = provisioning.secure_archive_database(name)
        provisioning.migrate_archive_database(name, login)
        assert provisioning.archive_revision(name, login) == provisioning.head_revision()
        engine = connect_as(name, login)
        with engine.connect() as conn:
            seed_chat(conn, chat_id=1, name="c", messages=[(1, 5, "hi")])  # an INSERT as the role; the counters trigger must fire under its ownership too
            assert conn.execute(sa.text("SELECT message_count FROM chats WHERE chat_id = 1")).scalar() == 1
        engine.dispose()

    def test_everything_the_schema_created_is_owned_by_the_role(self, server_ready):
        name = make_legacy_archive(900_042)
        login = provisioning.secure_archive_database(name)
        other_owners = scalar(
            name,
            None,
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner <> '" + login.role + "'",
        )
        assert other_owners == 0
        function_owners = scalar(
            name,
            None,
            "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = '" + login.role + "') "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')",
        )
        assert function_owners == 0

    def test_a_secured_archive_is_closed_to_the_other_archives(self, server_ready):
        legacy = make_legacy_archive(900_043)
        other = provisioning.provision_archive_database(900_044)
        login = provisioning.secure_archive_database(legacy)
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(legacy, other.login).connect()
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(other.db_name, login).connect()

    def test_securing_twice_works_and_replaces_the_password(self, server_ready):
        name = make_legacy_archive(900_045)
        first = provisioning.secure_archive_database(name)
        second = provisioning.secure_archive_database(name)
        assert first.role == second.role and first.password != second.password
        provisioning.verify_archive_login(name, second)
        with pytest.raises(OperationalError):
            connect_as(name, first).connect()  # the old password no longer works

    def test_a_database_name_that_is_not_a_plain_identifier_is_refused_before_any_ddl(self, server_ready):
        with pytest.raises(ValueError, match="Refusing"):
            provisioning.secure_archive_database('evil"; DROP DATABASE x; --')


class TestVerification:
    def test_verify_fails_for_a_wrong_password(self, two):
        a, _ = two
        with pytest.raises(OperationalError):
            provisioning.verify_archive_login(a.db_name, db.ArchiveLogin(role=a.login.role, password="wrong"))

    def test_verify_fails_for_the_wrong_database(self, two):
        a, b = two
        with pytest.raises(OperationalError):
            provisioning.verify_archive_login(b.db_name, a.login)


class TestConnectionLayer:
    def test_the_url_authenticates_as_the_role_only_when_given_a_login(self, server_ready):
        login = db.ArchiveLogin(role="r1", password="pw")
        assert db.server_database_url("d", login).username == "r1"
        assert db.server_database_url("d", login).password == "pw"
        assert db.server_database_url("d").username == sa.make_url(settings.control_database_url).username

    def test_each_login_has_its_own_pool_and_disposing_a_database_drops_all_of_them(self, two):
        a, _ = two
        shared = connection.get_tenant_engine(a.db_name)
        own = connection.get_tenant_engine(a.db_name, a.login)
        assert shared is not own
        assert connection.get_tenant_engine(a.db_name, a.login) is own
        db.dispose_tenant_engine(a.db_name)
        assert connection.get_tenant_engine(a.db_name, a.login) is not own  # a new pool was built: the old one was forgotten


class TestSecureArchivesScript:
    @pytest.fixture
    def accounts(self, control_conn, server_ready):
        return control_conn

    def add_account(self, conn, username: str, archive: str | None) -> int:
        user_id = conn.execute(sa.text("INSERT INTO users (username, password_hash) VALUES (:u, 'x') RETURNING id"), {"u": username}).scalar_one()
        conn.commit()
        if archive is not None:
            assert cq.set_archive_db_ref(conn, user_id, archive)
        return user_id

    def login_of(self, conn, user_id: int) -> db.ArchiveLogin | None:
        return archive_login_from_row(cq.get_archive_login_row(conn, user_id) or {})

    def test_it_secures_a_shared_login_archive_and_stores_a_working_encrypted_login(self, accounts, capsys):
        name = make_legacy_archive(900_050)
        uid = self.add_account(accounts, "alice", name)
        assert secure_archives.main([]) == 0
        login = self.login_of(accounts, uid)
        assert login is not None
        provisioning.verify_archive_login(name, login)
        row = cq.get_archive_login_row(accounts, uid)
        assert row is not None
        stored = row["archive_db_password"]
        assert stored != login.password and stored.startswith("gAAAA")  # a Fernet token, not the plaintext
        out = capsys.readouterr().out
        assert "secured" in out and login.password not in out and stored not in out

    def test_dry_run_reports_and_changes_nothing(self, accounts, capsys):
        name = make_legacy_archive(900_051)
        uid = self.add_account(accounts, "alice", name)
        assert secure_archives.main(["--dry-run"]) == 0
        assert "SHARED" in capsys.readouterr().out
        assert self.login_of(accounts, uid) is None
        assert scalar_param("SELECT COUNT(*) FROM pg_roles WHERE rolname = :r", r=name) == 0  # no role was created

    def test_a_second_run_leaves_a_secured_archive_alone(self, accounts, capsys):
        name = make_legacy_archive(900_052)
        uid = self.add_account(accounts, "alice", name)
        secure_archives.main([])
        before = cq.get_archive_login_row(accounts, uid)
        capsys.readouterr()
        assert secure_archives.main([]) == 0
        assert "has its own login" in capsys.readouterr().out
        assert cq.get_archive_login_row(accounts, uid) == before  # same password: it was not rotated

    def test_one_broken_archive_does_not_stop_the_others(self, accounts, capsys):
        good = make_legacy_archive(900_053)
        self.add_account(accounts, "ghost", "televault_test_does_not_exist")
        uid = self.add_account(accounts, "alice", good)
        assert secure_archives.main([]) == 1
        out = capsys.readouterr().out
        assert "FAILED   ghost" in out and "secured  alice" in out
        assert "does not exist" in out
        assert scalar_param("SELECT COUNT(*) FROM pg_roles WHERE rolname = 'televault_test_does_not_exist'") == 0  # no orphan role for a missing database
        assert self.login_of(accounts, uid) is not None

    def test_repair_replaces_a_login_that_no_longer_works(self, accounts, capsys):
        """The recovery path when FERNET_KEY was lost: the stored passwords cannot be decrypted, but the server login can still reset the roles."""
        name = make_legacy_archive(900_055)
        uid = self.add_account(accounts, "alice", name)
        secure_archives.main([])
        accounts.execute(sa.text("UPDATE users SET archive_db_password = 'garbage' WHERE id = :i"), {"i": uid})
        accounts.commit()
        capsys.readouterr()
        assert secure_archives.main(["--repair"]) == 0
        assert "secured  alice" in capsys.readouterr().out
        login = self.login_of(accounts, uid)
        assert login is not None
        provisioning.verify_archive_login(name, login)

    def test_repair_leaves_a_working_login_alone(self, accounts, capsys):
        name = make_legacy_archive(900_056)
        uid = self.add_account(accounts, "alice", name)
        secure_archives.main([])
        before = cq.get_archive_login_row(accounts, uid)
        assert secure_archives.main(["--repair"]) == 0
        assert cq.get_archive_login_row(accounts, uid) == before

    def test_a_login_that_no_longer_works_is_reported_not_ignored(self, accounts, capsys):
        name = make_legacy_archive(900_054)
        uid = self.add_account(accounts, "alice", name)
        secure_archives.main([])
        accounts.execute(sa.text("UPDATE users SET archive_db_password = 'garbage' WHERE id = :i"), {"i": uid})
        accounts.commit()
        capsys.readouterr()
        assert secure_archives.main([]) == 1
        assert "FAILED   alice" in capsys.readouterr().out


class TestLeastPrivilegedServerLogin:
    """TeleVault's own Postgres login need not be a superuser: CREATEDB + CREATEROLE is enough (db/provisioning.py's docstring says so, so test it)."""

    @pytest.fixture
    def limited_server(self, control_database):
        """Switch db/connection.py to a login with only the privileges in `privileges`, on the test control database; yields a function that does so."""
        from tests.conftest import RUN_ID, TEST_DB_PREFIX, _server_engine

        role = f"{TEST_DB_PREFIX}{RUN_ID}_limited_app"
        admin = _server_engine()

        def use(privileges: str) -> None:
            with admin.connect() as conn:
                exists = conn.execute(sa.text("SELECT COUNT(*) FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar() == 1
                verb = "ALTER" if exists else "CREATE"  # not DROP + CREATE: the role holds a grant on the control database, which blocks DROP ROLE
                conn.execute(sa.text(f"{verb} ROLE \"{role}\" LOGIN PASSWORD 'pw' {'CREATEDB' if 'CREATEDB' in privileges else 'NOCREATEDB'} {'CREATEROLE' if 'CREATEROLE' in privileges else 'NOCREATEROLE'}"))
                conn.execute(sa.text(f'GRANT CONNECT ON DATABASE "{control_database}" TO "{role}"'))
            db.close_tenant_engines()
            db.close_server()
            db.init_server(settings.control_database_url)
            db.init_server(sa.make_url(settings.control_database_url).set(username=role, password="pw").render_as_string(hide_password=False))

        yield use
        db.close_tenant_engines()
        db.close_server()
        admin.dispose()

    def test_createdb_and_createrole_are_enough_for_provisioning_and_isolation(self, limited_server):
        limited_server("CREATEDB CREATEROLE")
        a = provisioning.provision_archive_database(900_070)
        b = provisioning.provision_archive_database(900_071)
        assert scalar(a.db_name, a.login, "SELECT current_user") == a.login.role
        with pytest.raises(OperationalError, match="permission denied|CONNECT"):
            connect_as(b.db_name, a.login).connect()

    def test_without_createrole_provisioning_fails_with_the_documented_error_and_creates_nothing(self, limited_server):
        limited_server("CREATEDB")
        with pytest.raises(provisioning.ArchiveProvisioningError) as excinfo:
            provisioning.provision_archive_database(900_072)
        assert excinfo.value.reason == "provisioning_permission_denied" and "CREATEROLE" in str(excinfo.value)
        name = provisioning._tenant_database_name(900_072)
        assert scalar_param("SELECT COUNT(*) FROM pg_database WHERE datname = :d", d=name) == 0
