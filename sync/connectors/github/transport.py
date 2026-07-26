"""How the GitHub connector reaches GitHub, or a recorded copy of it.

The third transport, and the one that tests whether SDK v1 actually generalises
— which is the only reason to write it before the community does. GitHub differs
from both existing connectors in ways the contract has to absorb without
special-casing:

* **Link-header pagination.** Neither an offset like Jira nor an opaque cursor
  in the body like Slack. The next page is a URL in a response *header*, which
  is the first time a cursor has not been part of the payload at all.
* **Two rate limits.** A primary quota that resets at a stated time, and a
  secondary abuse limit that answers 403 with `Retry-After`. A 403 that means
  "slow down" and a 403 that means "you may not read this" arrive on the same
  status code and have to be told apart, because retrying one forever and
  dead-lettering the other are both wrong.
* **404 for private.** GitHub hides existence rather than admitting it, so a
  404 during a sync of a repository you were told about is a permissions
  answer, not a missing-resource answer.

Same shape as the other two: one call in, one decoded body out, HTTP status
mapped to the runtime's error taxonomy in exactly one place.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from sync.connectors.sdk import (
    PermanentSourceError,
    RateLimitedError,
    TransientSourceError,
)

API_ROOT = "https://api.github.com"

# GitHub asks for this and changes behaviour without it.
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"

# rel="next" out of a Link header. GitHub sends the whole set of rels, in an
# order it does not promise.
NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class Response:
    """A decoded body plus the one header that carries the cursor.

    A plain body is not enough here: for GitHub the resume token lives in
    `Link`, so a transport that returned only JSON would force the connector to
    reconstruct paging from item counts — which is exactly the guesswork that
    drops records at page boundaries.
    """

    __slots__ = ("body", "next_url")

    def __init__(self, body: Any, next_url: str | None = None) -> None:
        self.body = body
        self.next_url = next_url


class GitHubTransport(Protocol):
    """GitHub REST calls.

    `get` takes a full URL as well as a path, because following a Link header
    means following a URL GitHub built, query string and all. Rebuilding it from
    parts would mean assuming the shape of a cursor GitHub does not document.
    """

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Response: ...

    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...

    def patch(self, path: str, body: Mapping[str, Any]) -> Any: ...

    def delete(self, path: str) -> None: ...


class FixtureTransport:
    """Serves recorded responses from tests/fixtures/github.

    A path becomes a filename by replacing slashes with dots:

        orgs.acme.members.json
        repos.acme.web.issues.json

    Each file holds the complete dataset and this class does the paging, so a
    fixture stays readable and the connector's paging is still exercised.
    """

    def __init__(self, root: Path, *, page_size: int = 100) -> None:
        self._root = Path(root)
        self._page_size = max(1, page_size)
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.states: dict[str, str] = {}
        self.calls: list[str] = []

    @staticmethod
    def filename(path: str) -> str:
        return path.strip("/").replace("/", ".") + ".json"

    def _load(self, path: str) -> Any:
        file = self._root / self.filename(path)
        if not file.exists():
            # GitHub hides what you cannot see behind a 404, so this is the
            # honest fixture behaviour rather than an empty list.
            raise PermanentSourceError(f"404 for {path}")
        return json.loads(file.read_text(encoding="utf-8"))

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Response:
        self.calls.append(path)
        # A followed Link is a full URL; strip the root back off so a fixture
        # is addressed the same way whichever route reached it.
        clean = path.removeprefix(API_ROOT)
        page = 1
        if "?" in clean:
            clean, _, query = clean.partition("?")
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "page":
                    page = int(value or 1)

        body = self._load(clean)
        if not isinstance(body, list):
            return Response(self._decorate(clean, body))

        start = (page - 1) * self._page_size
        window = [self._decorate(clean, item) for item in body[start : start + self._page_size]]
        has_more = start + self._page_size < len(body)
        next_url = f"{API_ROOT}{clean}?page={page + 1}" if has_more else None
        return Response(window, next_url)

    def _decorate(self, path: str, item: Any) -> Any:
        """Apply write-backs recorded in this process, so a round trip is visible."""
        if not isinstance(item, dict):
            return item
        key = str(item.get("node_id") or item.get("id") or "")
        if key and key in self.states:
            item = {**item, "state": self.states[key]}
        return item

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(f"POST {path}")
        created = {"id": f"c-{len(self.comments.get(path, [])) + 1}", **dict(body)}
        self.comments.setdefault(path, []).append(created)
        return created

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(f"PATCH {path}")
        issue = self._load(path)
        key = str(issue.get("node_id") or issue.get("id") or "")
        if "state" in body:
            self.states[key] = str(body["state"])
        return {**issue, **dict(body)}

    def delete(self, path: str) -> None:
        self.calls.append(f"DELETE {path}")
        for comments in self.comments.values():
            for index, comment in enumerate(list(comments)):
                if path.endswith(str(comment.get("id"))):
                    comments.pop(index)
                    return


class HttpTransport:
    """The live one."""

    def __init__(self, token: str, *, base_url: str = API_ROOT, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Response:
        response = self._request("GET", path, params=params)
        link = response.headers.get("Link", "")
        match = NEXT_LINK.search(link)
        return Response(response.json(), match.group(1) if match else None)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._request("POST", path, json=body).json()

    def patch(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._request("PATCH", path, json=body).json()

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientSourceError(f"github timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientSourceError(f"github unreachable: {exc}") from exc

        if response.status_code == 429 or _is_rate_limited(response):
            raise RateLimitedError("github rate limited", retry_after=_retry_after(response))
        if response.status_code >= 500:
            raise TransientSourceError(f"github returned {response.status_code}")
        if response.status_code >= 400:
            raise PermanentSourceError(
                f"github returned {response.status_code} for {method} {path}"
            )
        return response


def _is_rate_limited(response: httpx.Response) -> bool:
    """Tell a throttling 403 from a forbidding one.

    GitHub answers both with 403 and they need opposite handling: retrying a
    permissions error forever burns the budget and never succeeds, while
    dead-lettering a throttle drops a stream that was only being asked to wait.
    Two signals, either of which is decisive — the abuse limiter sets
    Retry-After, and the primary quota sets its remaining count to zero.
    """
    if response.status_code != 403:
        return False
    if response.headers.get("Retry-After"):
        return True
    return bool(response.headers.get("X-RateLimit-Remaining") == "0")


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from whichever header GitHub used.

    Retry-After is a delay; X-RateLimit-Reset is an absolute epoch time. Both
    appear, never together, and confusing them means sleeping until 2026.
    """
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            return None

    reset = response.headers.get("X-RateLimit-Reset")
    if not reset:
        return None
    try:
        target = float(reset)
    except ValueError:
        return None
    server_time = response.headers.get("Date")
    now = _http_date(server_time) if server_time else None
    # GitHub's clock, not ours: a machine minutes out of sync would otherwise
    # sleep for minutes too long or not at all.
    return max(0.0, target - now) if now is not None else None


def _http_date(value: str) -> float | None:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
