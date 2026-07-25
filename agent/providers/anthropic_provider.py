"""The Anthropic provider, via the official SDK.

Three things about the current API that a provider written from memory gets
wrong, all of which are 400s rather than degradations:

**Sampling parameters are gone.** `temperature`, `top_p` and `top_k` are
rejected on Opus 5. Behaviour is steered by prompting, so there is nothing to
plumb through and the request model deliberately has no field for them.

**Thinking is on by default and shares the output budget.** `max_tokens` caps
thinking plus response text together, so a budget sized around the answer alone
truncates mid-sentence. Turning thinking off is allowed only at effort `high`
or below; pairing `disabled` with `xhigh` or `max` is rejected, which is why
core.config validates the pair rather than letting it fail at request time.

**A refusal is a successful response.** Safety classifiers can decline a
request and return HTTP 200 with `stop_reason: "refusal"` and empty or partial
content. Code that reads `content[0]` unconditionally breaks on it, so this
provider checks the stop reason first and surfaces a refusal as a normal
completion for the agent loop to handle rather than an exception.

Server-side refusal fallbacks are on by default: a declined request is re-run
on Anthropic's recommended fallback model inside the same call. For a system
whose whole job is answering questions over synced enterprise content, a
refusal that could have been served is a worse outcome than the cost of the
retry. It is Claude-API-only, so operators pointing at Bedrock or Vertex turn
it off.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx

from agent.providers.base import (
    Completion,
    CompletionRequest,
    PermanentProviderError,
    ProviderConfigError,
    ProviderRateLimitedError,
    StopReason,
    TransientProviderError,
    Usage,
)

LOG = logging.getLogger("hippo.agent.anthropic")

# The scalar form of server-side fallbacks, which routes by refusal category
# rather than making us maintain a model list.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
}


class AnthropicProvider:
    """Talks to the Messages API through the official SDK."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        effort: str = "high",
        thinking: str = "adaptive",
        refusal_fallback: bool = True,
        client: anthropic.Anthropic | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not model:
            msg = "an Anthropic model must be named; see HIPPO_MODEL"
            raise ProviderConfigError(msg)

        self.model = model
        self._effort = effort
        self._thinking = thinking
        self._refusal_fallback = refusal_fallback

        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if http_client is not None:
                kwargs["http_client"] = http_client
            self._client = anthropic.Anthropic(**kwargs)

    # -- request shaping ----------------------------------------------------

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "output_config": {"effort": self._effort},
        }
        if request.system is not None:
            payload["system"] = request.system
        if self._thinking == "adaptive":
            payload["thinking"] = {"type": "adaptive"}
        else:
            payload["thinking"] = {"type": "disabled"}
        return payload

    # -- completion ---------------------------------------------------------

    def complete(self, request: CompletionRequest) -> Completion:
        payload = self._payload(request)
        try:
            if self._refusal_fallback:
                response = self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **payload
                )
            else:
                response = self._client.messages.create(**payload)
        except anthropic.APIError as exc:
            raise _translate(exc) from exc

        stop_reason = _STOP_REASONS.get(str(response.stop_reason or ""), "other")
        text = _text_of(response)

        if stop_reason == "refusal":
            # Not an error: a successful response the agent loop has to handle.
            # Content is empty when the decline came before any output and
            # partial when it came mid-stream; either way it is not an answer.
            LOG.warning(
                "the model declined the request",
                extra={
                    "model": response.model,
                    "category": _refusal_category(response),
                    "partial_characters": len(text),
                },
            )

        return Completion(
            text=text,
            model=str(response.model),
            provider=self.name,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=int(response.usage.input_tokens),
                output_tokens=int(response.usage.output_tokens),
            ),
            raw={"stop_reason": str(response.stop_reason or "")},
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        """Exact, from the API. Never estimated with a foreign tokeniser."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
        }
        if request.system is not None:
            payload["system"] = request.system
        try:
            return int(self._client.messages.count_tokens(**payload).input_tokens)
        except anthropic.APIError as exc:
            raise _translate(exc) from exc


def _text_of(response: Any) -> str:
    """Text blocks only. Thinking blocks are not the answer."""
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(str(block.text))
    return "".join(parts)


def _refusal_category(response: Any) -> str | None:
    details = getattr(response, "stop_details", None)
    if details is None:
        return None
    return str(getattr(details, "category", None) or "") or None


def _translate(exc: anthropic.APIError) -> Exception:
    """Map the SDK's typed exceptions onto the three shapes a caller acts on.

    Most specific first: a rate limit and a 404 are both APIStatusError, and
    collapsing them would lose the only distinction that changes behaviour.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimitedError(str(exc), retry_after=_retry_after(exc))
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return PermanentProviderError(f"the model API rejected our credentials: {exc}")
    if isinstance(exc, anthropic.NotFoundError):
        return PermanentProviderError(f"unknown model or endpoint: {exc}")
    if isinstance(exc, anthropic.BadRequestError):
        return PermanentProviderError(f"the model API rejected the request: {exc}")
    if isinstance(exc, anthropic.APIConnectionError):
        return TransientProviderError(f"could not reach the model API: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            return TransientProviderError(f"model API error {exc.status_code}: {exc}")
        return PermanentProviderError(f"model API error {exc.status_code}: {exc}")
    return TransientProviderError(str(exc))


def _retry_after(exc: anthropic.RateLimitError) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
