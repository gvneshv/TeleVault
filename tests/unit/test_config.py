"""config.py - what is required, what has defaults, and how values are interpreted."""

from pathlib import Path

import pytest

import config


@pytest.fixture
def env(monkeypatch):
    """A clean slate: every setting config.py reads is removed, then required ones are put back."""
    for key in ("CONTROL_DATABASE_URL", "FERNET_KEY", "JWT_SECRET", "TELEVAULT_BOOTSTRAP_ADMIN_TOKEN",
                "ENABLE_API_DOCS", "DATA_DIR", "LOG_LEVEL", "LOG_FILE", "MEDIA_MAX_FILE_MB", "MEDIA_ACCOUNT_LIMIT_GB", "BACKFILL_COOLDOWN_HOURS",
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


class TestMediaLimits:
    def test_defaults_are_fifty_megabytes_per_file_and_ten_gigabytes_per_account(self, env):
        s = config._load()
        assert s.media_max_file_bytes == 50 * 1024 * 1024
        assert s.media_account_limit_bytes == 10 * 1024 * 1024 * 1024

    def test_values_are_read_in_mb_and_gb_and_kept_in_bytes(self, env):
        env.setenv("MEDIA_MAX_FILE_MB", "2")
        env.setenv("MEDIA_ACCOUNT_LIMIT_GB", "3")
        s = config._load()
        assert (s.media_max_file_bytes, s.media_account_limit_bytes) == (2 * 1024**2, 3 * 1024**3)

    @pytest.mark.parametrize("key", ["MEDIA_MAX_FILE_MB", "MEDIA_ACCOUNT_LIMIT_GB"])
    @pytest.mark.parametrize("bad", ["50MB", "1.5", "0", "-5"])
    def test_a_bad_limit_stops_startup_and_names_the_variable(self, env, key, bad):
        env.setenv(key, bad)
        with pytest.raises(SystemExit) as exc:
            config._load()
        assert key in str(exc.value)


class TestBackfillCooldown:
    def test_default_is_one_day(self, env):
        assert config._load().backfill_cooldown_seconds == 24 * 3600

    def test_hours_are_kept_in_seconds(self, env):
        env.setenv("BACKFILL_COOLDOWN_HOURS", "72")
        assert config._load().backfill_cooldown_seconds == 72 * 3600

    @pytest.mark.parametrize("hours, seconds", [("0", 0), ("168", 168 * 3600)])
    def test_zero_switches_it_off_and_a_week_is_the_ceiling(self, env, hours, seconds):
        env.setenv("BACKFILL_COOLDOWN_HOURS", hours)
        assert config._load().backfill_cooldown_seconds == seconds

    @pytest.mark.parametrize("bad", ["1d", "1.5", "-1", "169"])
    def test_a_bad_value_stops_startup_and_names_the_variable(self, env, bad):
        env.setenv("BACKFILL_COOLDOWN_HOURS", bad)
        with pytest.raises(SystemExit) as exc:
            config._load()
        assert "BACKFILL_COOLDOWN_HOURS" in str(exc.value)
