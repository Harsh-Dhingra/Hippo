"""Talking to Hippo over its own REST API.

The MCP server is a client, not a second implementation. It holds no database
credential, has no connection string, and cannot reach Postgres at all — the
only thing it can do is make authenticated HTTP requests as one person. That is
deliberate and it is what makes the surface safe to add: a surface cannot widen
the permission model because it has nothing to widen it with.

**One token, one person.** The token identifies whoever the coding agent is
working on behalf of, and every call runs as them. A shared team token would
collapse the permission model to a single principal and answer fluently while
doing it, which is the failure this whole project is built to prevent — so the
server refuses to start without a token and there is no anonymous mode.

**Failures say which kind they are.** A rejected token, an unreachable server
and a permission gap need different reactions from the person at the keyboard,
and "something went wrong" sends them to reset a password when the server is
down.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

LOG = logging.getLogger("hippo.surfaces.mcp.client")

DEFAULT_TIMEOUT = 60.0


class HippoError(Exception):
    """Something the person at the keyboard needs to know about."""


class NotAuthenticatedError(HippoError):
    """The token is missing, wrong, or expired."""


class NoAccessError(HippoError):
    """Authenticated, but this login is not linked to a source-system account.

    Distinct from "nothing matched" on purpose. A session with no principal can
    see nothing at all, and reporting that as an empty answer would be using
    "no results" to mean "no access".
    """


class UnavailableError(HippoError):
    """Hippo is unreachable or broken, rather than refusing."""


class HippoClient:
    """One person's view of one Hippo install."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise HippoError("HIPPO_URL is not set")
        if not token:
            # No anonymous mode, and no default token. Both would end with a
            # shared credential and a permission model that quietly means
            # nothing.
            raise HippoError(
                "HIPPO_TOKEN is not set. Each person needs their own token: a shared "
                "one would run every query as whoever it belongs to."
            )

        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise UnavailableError(f"could not reach Hippo at {self._base}: {exc}") from exc

        if response.status_code == 401:
            raise NotAuthenticatedError(
                "Hippo rejected this token. It may have expired; sign in again and "
                "set HIPPO_TOKEN to the new one."
            )
        if response.status_code == 403:
            raise NoAccessError(
                "This login is not linked to a Slack, Jira or GitHub account yet, so "
                "it can see nothing. An administrator links it after the next sync."
            )
        if response.status_code == 404:
            raise HippoError("Hippo has no such thing.")
        if response.status_code >= 500:
            raise UnavailableError(f"Hippo returned {response.status_code}")
        if response.status_code >= 400:
            raise HippoError(_detail(response))
        return response.json()

    # -- reading ------------------------------------------------------------

    async def retrieve(self, question: str, *, k: int = 12, hops: int = 1) -> dict[str, Any]:
        """What this person can see for a question. No model call."""
        body = await self._request(
            "POST", "/api/v1/retrieve", json={"question": question, "k": k, "hops": hops}
        )
        return dict(body)

    async def ask(self, question: str, *, k: int = 12) -> dict[str, Any]:
        """A cited answer, synthesised by Hippo's own model."""
        body = await self._request("POST", "/api/v1/queries", json={"question": question, "k": k})
        return dict(body)

    async def timeline(self, entity_id: UUID, *, hops: int = 2) -> dict[str, Any]:
        body = await self._request("GET", f"/api/v1/timeline/{entity_id}", params={"hops": hops})
        return dict(body)

    async def skills(self) -> list[dict[str, Any]]:
        return [dict(item) for item in await self._request("GET", "/api/v1/skills")]

    async def run_skill(self, name: str, inputs: dict[str, str]) -> dict[str, Any]:
        body = await self._request("POST", f"/api/v1/skills/{name}/run", json={"inputs": inputs})
        return dict(body)

    async def actions(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status_filter} if status_filter else {}
        return [dict(item) for item in await self._request("GET", "/api/v1/actions", params=params)]

    async def notes(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in await self._request("GET", "/api/v1/notes", params={"limit": limit})
        ]

    async def scopes(self) -> list[dict[str, Any]]:
        return [dict(item) for item in await self._request("GET", "/api/v1/scopes")]

    # -- writing ------------------------------------------------------------

    async def write_note(
        self, content: str, *, scope_id: str | None = None, about_entity: str | None = None
    ) -> dict[str, Any]:
        """Put something into memory deliberately.

        A note is the one place a person writes into memory directly, and it
        lands in a scope they own. Nothing here can write into somebody else's.
        """
        payload: dict[str, Any] = {"content": content}
        if scope_id:
            payload["scope_id"] = scope_id
        if about_entity:
            payload["about_entity"] = about_entity
        return dict(await self._request("POST", "/api/v1/notes", json=payload))

    async def whoami(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/api/v1/me"))


def _detail(response: httpx.Response) -> str:
    """The server's own message, when it gave one."""
    try:
        body = response.json()
    except ValueError:
        return f"Hippo returned {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else f"Hippo returned {response.status_code}"
