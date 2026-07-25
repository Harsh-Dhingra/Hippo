"""How the Jira connector reaches Jira, or a recorded copy of it.

Same seam as Slack, deliberately different underneath, which is the point of
having two connectors before stabilising the SDK in P3-SDK-1. Jira pages by
offset rather than by opaque cursor, authenticates with basic auth rather than
a bearer token, and addresses REST paths rather than method names. If the
contract in sdk.py only fitted Slack, this is where that would show.

The one thing kept identical is the shape: one call in, one decoded body out,
with HTTP status mapped to the runtime's error taxonomy in exactly one place.
"""

from __future__ import annotations

import base64
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

API_ROOT = "/rest/api/3"


class JiraTransport(Protocol):
    """One Jira REST call."""

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


# The field a Jira envelope puts its items in. Jira uses a different one per
# endpoint and is not going to stop.
LIST_KEYS = ("values", "issues", "comments")


class FixtureTransport:
    """Serves recorded responses from tests/fixtures/jira.

    A path becomes a filename by replacing slashes with dots:

        project.search.json
        group.member.g-dev.json
        issue.ACME-1.comment.json
        project.ACME.role.10001.json

    Each file holds the complete dataset and this class does the paging, so a
    fixture is readable as the thing it represents rather than pre-sliced into
    pages. It also means the same fixtures exercise every page size, which is
    where offset-pagination bugs live.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def filename(path: str, params: Mapping[str, Any] | None = None) -> str:
        parts = [path.strip("/").replace("/", ".")]
        # group/member identifies its group by query parameter, so the filename
        # has to carry it or every group would share one fixture.
        if params and params.get("groupId"):
            parts.append(str(params["groupId"]))
        return ".".join(parts) + ".json"

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((path, params))
        file = self._root / self.filename(path, params)
        if not file.is_file():
            msg = f"no recorded response for {path} (looked for {file.name})"
            raise PermanentSourceError(msg)

        body = json.loads(file.read_text(encoding="utf-8"))
        start_at = int(params.get("startAt", 0) or 0)
        max_results = int(params.get("maxResults", 50) or 50)

        if isinstance(body, list):
            # users/search answers with a bare array, no envelope.
            return body[start_at : start_at + max_results]

        for key in LIST_KEYS:
            if key in body:
                items = body[key]
                window = items[start_at : start_at + max_results]
                return {
                    **body,
                    key: window,
                    "startAt": start_at,
                    "maxResults": max_results,
                    "total": len(items),
                    "isLast": start_at + len(window) >= len(items),
                }

        # Role listings and the like are not paginated at all.
        return body


class HttpTransport:
    """The live path. Not exercised in CI, by design."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._auth_header = f"Basic {credentials}"
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{API_ROOT}/{path.strip('/')}"
        try:
            response = self._client.get(
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers={"Authorization": self._auth_header, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            msg = f"{path}: {exc}"
            raise TransientSourceError(msg) from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(f"{path}: rate limited", retry_after=_retry_after(response))
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            msg = f"{path}: HTTP {response.status_code}"
            raise TransientSourceError(msg)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # 401, 403 and 404 all mean this call will not start working on its
            # own. Retrying a permissions problem just delays the alert.
            msg = f"{path}: HTTP {response.status_code}"
            raise PermanentSourceError(msg)

        return response.json()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
