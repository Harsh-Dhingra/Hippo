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
    """Jira REST calls.

    The write verbs arrived with P1-SYNC-5. They are on the same protocol as
    `get` rather than a separate one because a connector that can write is
    already a connector that has to read the state it is about to change: rule
    3 makes inverse capture a precondition of execution, and capture is a GET.
    """

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...

    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...

    def delete(self, path: str) -> None: ...


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
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        # Transitions applied since this transport was built, so a rollback test
        # can observe the issue going back where it started.
        self.statuses: dict[str, str] = {}
        self._next_comment_id = 10_000

    @staticmethod
    def filename(path: str, params: Mapping[str, Any] | None = None) -> str:
        parts = [path.strip("/").replace("/", ".")]
        # group/member identifies its group by query parameter, so the filename
        # has to carry it or every group would share one fixture.
        if params and params.get("groupId"):
            parts.append(str(params["groupId"]))
        return ".".join(parts) + ".json"

    # Jira offers different transitions depending on where an issue currently
    # is, so a static fixture cannot answer this: after a transition to Done,
    # the way back to In Progress has to be on offer or a rollback is
    # untestable. Derived from the same mutable status the writes update.
    STATUSES = ("To Do", "In Progress", "Done")

    def _current_status(self, issue: str) -> str:
        if issue in self.statuses:
            return self.statuses[issue]
        file = self._root / f"issue.{issue}.json"
        if not file.is_file():
            return ""
        fields = json.loads(file.read_text(encoding="utf-8")).get("fields", {})
        return str((fields.get("status") or {}).get("name") or "")

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((path, params))

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "issue" and parts[2] == "transitions":
            current = self._current_status(parts[1])
            return {
                "transitions": [
                    {"id": str(20 + index), "to": {"name": name}}
                    for index, name in enumerate(self.STATUSES)
                    if name != current
                ]
            }

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

        # An issue read reflects transitions applied since this transport was
        # built, so a second capture_inverse sees what the first write did.
        if len(parts) == 2 and parts[0] == "issue" and isinstance(body, dict):
            applied = self.statuses.get(parts[1])
            if applied is not None:
                fields = {**dict(body.get("fields") or {}), "status": {"name": applied}}
                return {**body, "fields": fields}

        # Role listings and the like are not paginated at all.
        return body

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        """Record the write, and answer the way Jira does.

        Enough of a simulation to test rollback rather than just execution:
        posting a comment yields an id that the matching delete then has to
        name, and a transition changes what the next capture_inverse reads.
        """
        self.writes.append((path, dict(body)))
        if path.endswith("/comment"):
            self._next_comment_id += 1
            return {"id": str(self._next_comment_id), "body": body.get("body")}
        if path.endswith("/transitions"):
            issue = path.split("/")[1]
            self.statuses[issue] = str(dict(body).get("transition", {}).get("name", ""))
            return {}
        msg = f"no recorded write for {path}"
        raise PermanentSourceError(msg)

    def delete(self, path: str) -> None:
        self.deletes.append(path)


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
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._request("POST", path, json=dict(body))

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        """One place that maps HTTP status to this project's error taxonomy.

        Shared by every verb on purpose. A write path with its own idea of
        which failures are retryable is a write path that retries a 403 forever
        or gives up on a 502 — and for an approved action, the difference is
        whether a human's decision quietly evaporates.
        """
        url = f"{self._base_url}{API_ROOT}/{path.strip('/')}"
        try:
            response = self._client.request(
                method,
                url,
                params=None
                if params is None
                else {k: v for k, v in params.items() if v is not None},
                json=json,
                headers={"Authorization": self._auth_header, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            msg = f"{method} {path}: {exc}"
            raise TransientSourceError(msg) from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(
                f"{method} {path}: rate limited", retry_after=_retry_after(response)
            )
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            msg = f"{method} {path}: HTTP {response.status_code}"
            raise TransientSourceError(msg)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # 401, 403 and 404 all mean this call will not start working on its
            # own. Retrying a permissions problem just delays the alert.
            msg = f"{method} {path}: HTTP {response.status_code}"
            raise PermanentSourceError(msg)

        # 204 on a delete, and Jira's transition endpoint answers empty too.
        if not response.content:
            return None
        return response.json()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
