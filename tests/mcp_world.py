"""Two people, one corpus, and an MCP surface for each.

The end-to-end shape the fragment claims: a coding agent reaching Hippo over
HTTP with one person's token, and seeing exactly what that person sees. Built
here rather than inline because it needs a running app, two users with
principals, and content granted asymmetrically — and the test that uses it
should read as three lines.

The client is pointed at the app through httpx's ASGI transport, so the request
goes through the real routes, the real auth and the real permission filter
without a socket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.providers.base import Completion, CompletionRequest, Usage
from api.auth import create_user, login
from api.main import create_app
from core.config import Settings
from core.db import Connection, connect
from resolver.embeddings import HashingEmbeddings
from surfaces.mcp.client import HippoClient

ORG_SCOPE = "00000000-0000-0000-0000-000000000001"


class QuietModel:
    name = "quiet"
    model = "quiet-1"

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(
            text="Nothing to add.",
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


def _person(conn: Connection, email: str, source_id: str) -> UUID:
    principal = uuid4()
    conn.execute(
        "INSERT INTO principals (id, kind, email, source_id) VALUES (%s, 'user', %s, %s)",
        (principal, email, source_id),
    )
    create_user(conn, email, "a-password")
    return principal


def _grant(conn: Connection, principal: UUID, text: str) -> None:
    entity = uuid4()
    conn.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'note')", (entity,)
    )
    conn.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (entity, principal),
    )
    conn.execute(
        "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) VALUES (%s, %s, %s, 0)",
        (entity, ORG_SCOPE, text),
    )


@asynccontextmanager
async def two_people(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[HippoClient, HippoClient]]:
    """An MCP client for Alice and one for Bob, against one running Hippo."""
    monkeypatch.setattr("api.main.build_provider", lambda _settings: QuietModel())
    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())

    settings = Settings(database_url=dsn, log_level="WARNING", service_name="hippo-test")
    app = create_app(settings)

    tokens: dict[str, str] = {}
    with TestClient(app):
        with connect(dsn) as conn:
            alice = _person(conn, "alice@example.com", "U-ALICE")
            bob = _person(conn, "bob@example.com", "U-BOB")
            _grant(conn, alice, "alice's private note about the liability cap on the renewal")
            _grant(conn, bob, "bob's private note about the liability cap on the renewal")
            conn.commit()
            for email in ("alice@example.com", "bob@example.com"):
                tokens[email] = login(conn, email, "a-password").token
            conn.commit()

        transport = httpx.ASGITransport(app=app)
        clients = {
            email: HippoClient(
                "http://hippo.test",
                token,
                http=httpx.AsyncClient(
                    base_url="http://hippo.test",
                    transport=transport,
                    headers={"Authorization": f"Bearer {token}"},
                ),
            )
            for email, token in tokens.items()
        }
        try:
            yield clients["alice@example.com"], clients["bob@example.com"]
        finally:
            for client in clients.values():
                await client.aclose()
