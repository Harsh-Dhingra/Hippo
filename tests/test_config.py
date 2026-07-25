"""Settings behaviour: defaults, environment binding, and refusing bad values."""

import pytest
from pydantic import ValidationError

from core.config import Settings, get_settings


def test_defaults_are_usable_without_any_environment() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "INFO"
    assert settings.service_name == "hippo-api"
    assert settings.pool_min_size == 1
    assert settings.pool_max_size == 10
    assert settings.migrate_on_startup is True


def test_reads_hippo_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_DATABASE_URL", "postgresql://db:5432/somewhere")
    monkeypatch.setenv("HIPPO_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("HIPPO_MIGRATE_ON_STARTUP", "false")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == "postgresql://db:5432/somewhere"
    assert settings.log_level == "DEBUG"
    assert settings.migrate_on_startup is False


def test_ignores_unprefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://leaked:5432/nope")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "leaked" not in settings.database_url


def test_rejects_unknown_log_level() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="CHATTY", _env_file=None)  # type: ignore[arg-type, call-arg]


def test_rejects_pool_max_below_min() -> None:
    with pytest.raises(ValidationError, match="pool_max_size"):
        Settings(pool_min_size=5, pool_max_size=2, _env_file=None)  # type: ignore[call-arg]


def test_rejects_non_positive_pool_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings(pool_open_timeout=0, _env_file=None)  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
