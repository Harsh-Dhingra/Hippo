"""Runtime configuration.

Secrets come from the environment or a mounted file, never from the repo and
never from the database. See CLAUDE.md, Conventions.
"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Process configuration, read from HIPPO_-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="HIPPO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql://localhost:5432/hippo",
        description="libpq connection string. Carries the only credential this process needs.",
    )
    service_name: str = Field(default="hippo-api", description="Value stamped on every log line.")
    log_level: LogLevel = Field(default="INFO")
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    pool_open_timeout: float = Field(default=10.0, gt=0)
    migrate_on_startup: bool = Field(
        default=True,
        description="Apply pending migrations when the API boots. Keeps `docker compose up` "
        "to a single step; set false when migrations are run as their own deploy stage.",
    )

    @model_validator(mode="after")
    def _check_pool_bounds(self) -> Self:
        if self.pool_max_size < self.pool_min_size:
            msg = (
                f"pool_max_size ({self.pool_max_size}) must be >= "
                f"pool_min_size ({self.pool_min_size})"
            )
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Cached; call `get_settings.cache_clear()` in tests."""
    return Settings()
