"""Runtime configuration.

Secrets come from the environment or a mounted file, never from the repo and
never from the database. See CLAUDE.md, Conventions.

Two of those secrets are typed as SecretStr, and the database URL is kept out
of this object's repr. That is not decoration: a Settings instance ends up in
log lines, in tracebacks, and in whatever a debugger prints, and pydantic's
default repr spells out every field it holds. Discovering that from a support
bundle is a bad way to discover it.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
EmbeddingProvider = Literal["hashing", "openai"]
ModelProviderName = Literal["anthropic", "openai"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingMode = Literal["adaptive", "disabled"]


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
        # Kept out of the repr rather than typed as SecretStr: every database
        # call needs the plain string, and a .get_secret_value() at each of
        # those call sites would be noise that teaches nobody anything. What
        # matters is that printing the settings does not print the password.
        repr=False,
    )
    service_name: str = Field(default="hippo-api", description="Value stamped on every log line.")
    log_level: LogLevel = Field(default="INFO")
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    pool_open_timeout: float = Field(default=10.0, gt=0)
    risk_policy_path: Path | None = Field(
        default=None,
        description="TOML file classifying action types as routine or consequential. "
        "Absent means the safest policy there is: everything waits for a human.",
    )
    alert_webhook_url: SecretStr = Field(
        default=SecretStr(""),
        description="Where to POST drift and sync-failure alerts. A SecretStr because a "
        "Slack incoming-webhook URL is a credential: anyone holding it can post as the "
        "integration. Empty disables delivery; the alerts are still recorded and shown.",
    )
    alert_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How often the API delivers open alerts to the webhook.",
    )
    auto_approve_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often the API applies the risk policy to pending actions. "
        "Only runs at all when the policy opts in; see agent/policy.py.",
    )
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
        default="mxbai-embed-large",
        description="Used when embedding_provider is 'openai'. Measured rather than guessed: "
        "`python -m evals.embeddings` compares candidates on the seeded eval corpus, and "
        "this one won on overall recall. 1024 dimensions, so it needs no migration. "
        "Ignored by the default 'hashing' provider, which needs no endpoint at all.",
    )
    embedding_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Any endpoint speaking the OpenAI /embeddings shape, including vLLM, "
        "Ollama and LM Studio.",
    )
    embedding_api_key: SecretStr = Field(
        default=SecretStr(""), description="From the environment, never the repo."
    )
    embedding_dimensions: int = Field(
        default=1024,
        ge=1,
        description="Must match the vector width in the chunks table.",
    )

    # --- Model provider ---------------------------------------------------
    # Pluggable permanently (STACK.md): a self-hoster who cannot point this at
    # their own inference endpoint has not really self-hosted anything.
    model_provider: ModelProviderName = Field(default="anthropic")
    model: str = Field(
        default="claude-opus-5",
        description="Model id. Anthropic ids are bare, with no date suffix.",
    )
    model_api_key: SecretStr = Field(
        default=SecretStr(""), description="From the environment, never the repo."
    )
    model_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Only read by the openai-compatible provider. Any endpoint speaking the "
        "chat-completions shape, including vLLM, Ollama and LM Studio.",
    )
    model_max_tokens: int = Field(
        default=4096,
        gt=0,
        description="Caps thinking and response text together on current Anthropic models, "
        "so a budget sized around the answer alone will truncate.",
    )
    model_effort: Effort = Field(default="high")
    model_thinking: ThinkingMode = Field(default="adaptive")
    model_refusal_fallback: bool = Field(
        default=True,
        description="Re-run a declined request on the recommended fallback model. "
        "Claude API only; turn off when pointing at Bedrock, Vertex or Foundry.",
    )

    @model_validator(mode="after")
    def _check_model_config(self) -> Self:
        """Catch the combination the API rejects, before a request is sent.

        Current Anthropic models allow thinking to be turned off only at effort
        `high` or below; pairing `disabled` with `xhigh` or `max` returns a 400.
        Failing here names the problem instead of surfacing it as a request
        error under load.
        """
        if (
            self.model_provider == "anthropic"
            and self.model_thinking == "disabled"
            and self.model_effort in ("xhigh", "max")
        ):
            msg = (
                f"model_thinking='disabled' is not allowed at model_effort="
                f"'{self.model_effort}'. Use effort 'high' or below, or leave "
                f"thinking adaptive."
            )
            raise ValueError(msg)
        return self

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
