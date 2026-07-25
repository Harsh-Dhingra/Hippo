"""The OpenAI-compatible provider.

Targets the `/chat/completions` request shape rather than any one vendor's SDK,
which is what makes it the local-model path: vLLM, Ollama, LM Studio and a
dozen hosted providers all speak it, so the self-hoster who wants the model on
their own hardware changes a base URL. That is the same reasoning as the
embedding provider in resolver/embeddings.py, and the same reason STACK.md
calls pluggability permanent.

Deliberately free of any Anthropic SDK import. The two providers share the
contract in base.py and nothing else; mixing one vendor's client into the other
vendor's path is how an abstraction quietly stops being one.
"""

from __future__ import annotations

import logging
from typing import Any

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

LOG = logging.getLogger("hippo.agent.openai")

# Rough characters per token for English prose. Only used when the endpoint
# offers no way to count, and reported as an estimate so nobody budgets a
# prompt against it as though it were exact.
CHARS_PER_TOKEN = 4

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class OpenAICompatibleProvider:
    """Any endpoint speaking the OpenAI chat-completions shape."""

    name = "openai"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not model:
            msg = "a model must be named; see HIPPO_MODEL"
            raise ProviderConfigError(msg)
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def _body(self, request: CompletionRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system is not None:
            # This shape carries the system prompt as a message rather than a
            # separate field. It is still the operator channel; retrieved
            # content stays in user turns (CLAUDE.md rule 6).
            messages.append({"role": "system", "content": request.system})
        messages.extend(
            {"role": message.role, "content": message.content} for message in request.messages
        )
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }

    def complete(self, request: CompletionRequest) -> Completion:
        body = self._body(request)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            msg = f"could not reach the model endpoint: {exc}"
            raise TransientProviderError(msg) from exc

        _raise_for_status(response)
        payload = response.json()

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = f"model response has no choices: {str(payload)[:200]}"
            raise TransientProviderError(msg)

        choice = choices[0]
        finish = str(choice.get("finish_reason") or "")
        stop_reason = _FINISH_REASONS.get(finish, "other")
        text = str((choice.get("message") or {}).get("content") or "")

        if stop_reason == "refusal":
            LOG.warning(
                "the model declined the request",
                extra={"model": payload.get("model"), "finish_reason": finish},
            )

        usage = payload.get("usage") or {}
        return Completion(
            text=text,
            model=str(payload.get("model") or self.model),
            provider=self.name,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
            ),
            raw={"finish_reason": finish},
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        """An estimate, and named as one.

        There is no counting endpoint in this shape, and a foreign tokeniser
        would be confidently wrong rather than roughly right. A caller budgeting
        a prompt should leave headroom accordingly.
        """
        characters = len(request.system or "")
        characters += sum(len(message.content) for message in request.messages)
        return max(1, characters // CHARS_PER_TOKEN)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise ProviderRateLimitedError(
            f"model endpoint rate limited: {response.text[:200]}",
            retry_after=_retry_after(response),
        )
    if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
        msg = f"model endpoint returned {response.status_code}: {response.text[:200]}"
        raise TransientProviderError(msg)
    if response.status_code >= httpx.codes.BAD_REQUEST:
        msg = f"model endpoint returned {response.status_code}: {response.text[:200]}"
        raise PermanentProviderError(msg)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
