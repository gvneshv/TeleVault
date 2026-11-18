"""config.py - what is required, what has defaults, and how values are interpreted."""

from pathlib import Path

import pytest

import config


@pytest.fixture
def env(monkeypatch):
    """A clean slate: every setting config.py reads is removed, then required ones are put back."""
    for key in ("CONTROL_DATABASE_URL", "FERNET_KEY", "JWT_SECRET", "TELEVAULT_BOOTSTRAP_ADMIN_TOKEN",
                "ENABLE_API_DOCS", "DATA_DIR", "LOG_LEVEL", "LOG_FILE",
                "TELEGRAM_DEVICE_MODEL", "TELEGRAM_SYSTEM_VERSION", "TELEGRAM_APP_VERSION"):
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


# --- How the client names itself to Telegram (Settings -> Devices) --------------------------------------------------------------------

def test_device_identity_defaults_are_plain_and_never_empty(env):
    s = config._load()
    assert s.telegram_device_model == "TeleVault Server"
    assert s.telegram_app_version == config.APP_VERSION
    assert s.telegram_system_version.strip()
    for value in (s.telegram_device_model, s.telegram_system_version, s.telegram_app_version):
        assert not any(bad in value.lower() for bad in ("unknown", "telethon", "pyrogram"))


@pytest.mark.parametrize("system, release, expected", [
    ("Linux", "6.8.0-45-generic", "Linux 6.8.0"),   # the distribution suffix is noise in a device list
    ("Windows", "11", "Windows 11"),
    ("", "", "Linux"),                              # never empty
])
def test_default_system_version_reads_like_a_person_would_write_it(monkeypatch, system, release, expected):
    monkeypatch.setattr(config.platform, "system", lambda: system)
    monkeypatch.setattr(config.platform, "release", lambda: release)
    assert config._default_system_version() == expected


def test_device_identity_can_be_overridden_and_a_blank_override_falls_back(env):
    env.setenv("TELEGRAM_DEVICE_MODEL", "  Home NAS ")
    env.setenv("TELEGRAM_SYSTEM_VERSION", "Debian 12")
    env.setenv("TELEGRAM_APP_VERSION", "   ")
    s = config._load()
    assert (s.telegram_device_model, s.telegram_system_version) == ("Home NAS", "Debian 12")
    assert s.telegram_app_version == config.APP_VERSION
