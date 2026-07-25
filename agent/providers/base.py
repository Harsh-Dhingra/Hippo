"""The model provider contract.

One interface, two implementations. ARCHITECTURE section 7 says the agent
service is the only component that talks to a model, and STACK.md says
pluggability is permanent with no graduation trigger, because a self-hoster who
cannot point this at their own inference endpoint has not really self-hosted
anything.

The contract is deliberately narrower than either underlying API. It carries
what the agent loop and the trace need and nothing else: a system prompt, a
message list, a completion, and honest token counts. Features that only one
provider has stay behind the provider, so a second implementation never widens
the interface for everyone.

Requests and completions are plain models rather than provider objects so that
P1-AGT-4's trace can store exactly what went into a prompt. ARCHITECTURE
section 9 makes that auditability the security story: the only egress is the
model API, and the trace is what makes it inspectable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant"]

# Where a completion stopped. Normalised across providers, because the agent
# loop has to branch on it and should not learn two vocabularies.
StopReason = Literal["end_turn", "max_tokens", "refusal", "other"]


class Message(BaseModel):
    """One turn."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = Field(min_length=1)


class CompletionRequest(BaseModel):
    """What the agent asks for.

    `system` is separate from `messages` because both providers treat it as a
    distinct channel, and because CLAUDE.md rule 6 depends on that separation:
    retrieved content is untrusted data that goes in a user turn, never in the
    system prompt where it would read as instruction.
    """

    model_config = ConfigDict(frozen=True)

    messages: tuple[Message, ...] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=4096, gt=0)

    def with_messages(self, messages: Sequence[Message]) -> CompletionRequest:
        return self.model_copy(update={"messages": tuple(messages)})


class Usage(BaseModel):
    """Token counts, for the trace and for the token-spend metric in P2-OBS-1."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Completion(BaseModel):
    """What came back."""

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    provider: str
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    # Anything provider-specific worth keeping for the trace. Never read by the
    # agent loop: the moment it is, it belongs in the contract instead.
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """A model call failed.

    One family, three shapes, because the only decision a caller makes is
    whether to retry. The jobs runtime owns backoff and the dead letter, so
    anything finer than retryable / not / slow-down would duplicate a policy
    that is already made one layer up.
    """


class ProviderConfigError(ProviderError):
    """The provider is misconfigured. Retrying will not help."""


class TransientProviderError(ProviderError):
    """A timeout, a 5xx, a dropped connection. Worth retrying."""


class PermanentProviderError(ProviderError):
    """Bad credentials, unknown model, a malformed request. Not worth retrying."""


class ProviderRateLimitedError(TransientProviderError):
    """Slow down. Carries the provider's own hint when it gives one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ModelProvider(Protocol):
    """What the agent service is allowed to assume about a model."""

    name: str
    model: str

    def complete(self, request: CompletionRequest) -> Completion: ...

    def count_tokens(self, request: CompletionRequest) -> int:
        """Input tokens this request would cost, before sending it.

        Used to keep a prompt inside its budget rather than discovering the
        limit by being truncated. Providers that cannot count without sending
        estimate, and say so.
        """
        ...
