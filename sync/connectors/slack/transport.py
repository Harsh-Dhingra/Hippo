"""How the Slack connector reaches Slack, or a recorded copy of it.

One seam, two implementations. The connector's logic is identical either way,
so what CI exercises against fixtures is the same code that runs against the
live workspace. A connector whose fixture path and live path diverge is a
connector whose tests prove nothing.

This is also the only place that knows about HTTP status codes, so mapping a
429 onto the runtime's backoff is done once rather than in every stream.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from sync.connectors.sdk import (
    PermanentSourceError,
    RateLimitedError,
    TransientSourceError,
)

SLACK_API = "https://slack.com/api"

# Slack errors that will never succeed on retry. Anything else is treated as
# transient, which is the safe direction: a retry costs time, a wrongly
# permanent failure costs the record.
FATAL_SLACK_ERRORS = frozenset(
    {
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "no_permission",
        "missing_scope",
        "channel_not_found",
        "user_not_found",
        "thread_not_found",
    }
)


class SlackTransport(Protocol):
    """One Slack Web API method call."""

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]: ...


class FixtureTransport:
    """Serves recorded responses from tests/fixtures/slack.

    Files are named after the call they answer, and each holds a mapping of
    request cursor to response, so pagination is exercised offline exactly as
    it happens live:

        users.list.json                      {"": {...}, "page2": {...}}
        conversations.history.C-DEALS.json
        conversations.replies.C-GENERAL.1750000000.000100.json
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def filename(method: str, params: Mapping[str, Any]) -> str:
        parts = [method]
        if "channel" in params:
            parts.append(str(params["channel"]))
        if "ts" in params:
            parts.append(str(params["ts"]))
        return ".".join(parts) + ".json"

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        path = self._root / self.filename(method, params)
        if not path.is_file():
            msg = f"no recorded response for {method} {dict(params)} (looked for {path.name})"
            raise PermanentSourceError(msg)

        pages = json.loads(path.read_text(encoding="utf-8"))
        cursor = str(params.get("cursor", "") or "")
        if cursor not in pages:
            msg = f"{path.name} has no page for cursor {cursor!r}"
            raise PermanentSourceError(msg)
        response: dict[str, Any] = pages[cursor]
        return response


class HttpTransport:
    """The live path. Not exercised in CI, by design (CLAUDE.md, fixtures before live)."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = SLACK_API,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self._base_url}/{method}",
                params={k: v for k, v in params.items() if v not in (None, "")},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            msg = f"{method}: {exc}"
            raise TransientSourceError(msg) from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(f"{method}: rate limited", retry_after=_retry_after(response))
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            msg = f"{method}: HTTP {response.status_code}"
            raise TransientSourceError(msg)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            msg = f"{method}: HTTP {response.status_code}"
            raise PermanentSourceError(msg)

        body: dict[str, Any] = response.json()
        if not body.get("ok", False):
            error = str(body.get("error", "unknown_error"))
            msg = f"{method}: {error}"
            if error == "ratelimited":
                raise RateLimitedError(msg, retry_after=_retry_after(response))
            if error in FATAL_SLACK_ERRORS:
                raise PermanentSourceError(msg)
            raise TransientSourceError(msg)
        return body


def _retry_after(response: httpx.Response) -> float | None:
    """Slack's own hint, when it gives one. Guessing is worse than waiting."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
