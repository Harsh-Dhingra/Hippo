"""Runtime configuration.

Secrets come from the environment or a mounted file, never from the repo and
never from the database. See CLAUDE.md, Conventions.
"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
EmbeddingProvider = Literal["hashing", "openai"]


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

    # --- Embeddings -------------------------------------------------------
    # Config-abstracted per STACK.md, so changing the model is a resolver
    # re-run rather than a migration. The default needs no service, which is
    # what lets the project run before anyone has chosen a model. It is
    # lexical, not semantic; see resolver/embeddings.py.
    embedding_provider: EmbeddingProvider = Field(default="hashing")
    embedding_model: str = Field(
        default="",
        description="Required when embedding_provider is 'openai'. Left empty on purpose: "
        "STACK.md defers the pick to a measured eval, and a guessed default would ship "
        "an unmeasured one to every adopter.",
    )
    embedding_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Any endpoint speaking the OpenAI /embeddings shape, including vLLM, "
        "Ollama and LM Studio.",
    )
    embedding_api_key: str = Field(default="", description="From the environment, never the repo.")
    embedding_dimensions: int = Field(
        default=1024,
        ge=1,
        description="Must match the vector width in the chunks table.",
    )

    @model_validator(mode="after")
    def _check_embedding_config(self) -> Self:
        if self.embedding_provider == "openai" and not self.embedding_model:
            msg = "embedding_model is required when embedding_provider is 'openai'"
            raise ValueError(msg)
        return self

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
