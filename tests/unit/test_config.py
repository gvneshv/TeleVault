"""config.py - what is required, what has defaults, and how values are interpreted."""

from pathlib import Path

import pytest

import config


@pytest.fixture
def env(monkeypatch):
    """A clean slate: every setting config.py reads is removed, then required ones are put back."""
    for key in ("CONTROL_DATABASE_URL", "FERNET_KEY", "JWT_SECRET", "TELEVAULT_BOOTSTRAP_ADMIN_TOKEN",
                "ENABLE_API_DOCS", "DATA_DIR", "LOG_LEVEL", "LOG_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FERNET_KEY", "k")
    monkeypatch.setenv("JWT_SECRET", "s")
    return monkeypatch


@pytest.mark.parametrize("missing", ["FERNET_KEY", "JWT_SECRET"])
def test_required_secrets_fail_loudly_when_missing(env, missing):
    env.delenv(missing)
    with pytest.raises(SystemExit) as exc:
        config._load()
    assert missing in str(exc.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_required_secret_counts_as_missing(env, blank):
    env.setenv("JWT_SECRET", blank)
    with pytest.raises(SystemExit):
        config._load()


def test_defaults(env):
    s = config._load()
    assert s.control_database_url == "postgresql+psycopg://televault:televault@localhost:5432/televault_control"
    assert s.log_level == "INFO"
    assert s.log_file is None
    assert s.bootstrap_admin_token is None
    assert s.enable_api_docs is False


def test_blank_bootstrap_token_is_none_not_empty_string(env):
    env.setenv("TELEVAULT_BOOTSTRAP_ADMIN_TOKEN", "   ")
    assert config._load().bootstrap_admin_token is None


def test_bootstrap_token_is_read_and_stripped(env):
    env.setenv("TELEVAULT_BOOTSTRAP_ADMIN_TOKEN", "  phrase  ")
    assert config._load().bootstrap_admin_token == "phrase"


@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
                                              ("false", False), ("0", False), ("no", False), ("nonsense", False), ("", False)])
def test_api_docs_flag_parsing(env, value, expected):
    env.setenv("ENABLE_API_DOCS", value)
    assert config._load().enable_api_docs is expected


def test_relative_data_dir_is_anchored_to_the_repo_not_the_working_directory(env, tmp_path):
    env.chdir(tmp_path)
    env.setenv("DATA_DIR", "somewhere")
    repo_root = Path(config.__file__).resolve().parent
    assert Path(config._load().data_dir) == (repo_root / "somewhere").resolve()


def test_absolute_data_dir_is_kept(env, tmp_path):
    env.setenv("DATA_DIR", str(tmp_path))
    assert Path(config._load().data_dir) == tmp_path.resolve()


def test_settings_are_frozen():
    with pytest.raises(Exception):
        config.settings.jwt_secret = "changed"  # type: ignore[misc]
