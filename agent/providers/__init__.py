"""Model providers.

One contract in base.py, two implementations, and one place that knows which
is configured. Everything above this package works against ModelProvider and
never learns which vendor answered.
"""

from agent.providers.anthropic_provider import AnthropicProvider
from agent.providers.base import (
    Completion,
    CompletionRequest,
    Message,
    ModelProvider,
    PermanentProviderError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitedError,
    TransientProviderError,
    Usage,
)
from agent.providers.openai_compatible import OpenAICompatibleProvider
from core.config import Settings

__all__ = [
    "AnthropicProvider",
    "Completion",
    "CompletionRequest",
    "Message",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "PermanentProviderError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRateLimitedError",
    "TransientProviderError",
    "Usage",
    "build_provider",
]


def build_provider(settings: Settings) -> ModelProvider:
    """The configured provider. The only place that maps config to a class."""
    if settings.model_provider == "openai":
        return OpenAICompatibleProvider(
            settings.model,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
        )
    return AnthropicProvider(
        settings.model,
        api_key=settings.model_api_key,
        effort=settings.model_effort,
        thinking=settings.model_thinking,
        refusal_fallback=settings.model_refusal_fallback,
    )
