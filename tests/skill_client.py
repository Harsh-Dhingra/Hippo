"""A running API with a skills directory, for the HTTP-level skill tests.

Its own module rather than a fixture in the test file because building the app
needs a database, a registered user with a principal, and a model that does not
call anything — and threading all three through each test reads worse than one
context manager.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.providers.base import Completion, CompletionRequest, Usage
from api.auth import create_user, login
from api.main import create_app
from core.config import Settings
from core.db import connect
from resolver.embeddings import HashingEmbeddings


class QuietModel:
    """Answers without saying anything interesting.

    These tests are about routing, validation and refusal, not about what a
    model produces — so the model is the least interesting thing in them.
    """

    name = "quiet"
    model = "quiet-1"

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(
            text="Nothing found.",
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@contextmanager
def client_for(
    dsn: str, skills: Path | None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
    """A client and a bearer token for a user who exists.

    Patched where main.py builds the model and the embedder, so everything
    between the HTTP boundary and the database is the code that ships.
    """
    monkeypatch.setattr("api.main.build_provider", lambda _settings: QuietModel())
    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())

    settings = Settings(
        database_url=dsn,
        log_level="WARNING",
        service_name="hippo-test",
        skills_path=skills,
    )

    with TestClient(create_app(settings)) as client:
        email = f"skills-{uuid4().hex[:8]}@example.com"
        with connect(dsn) as conn:
            # A principal first, so the user is linked on creation. Without one
            # every request is a 403 before it reaches the route, which is
            # correct behaviour and makes for a test that proves nothing.
            conn.execute(
                "INSERT INTO principals (id, kind, email, source_id) VALUES (%s, 'user', %s, %s)",
                (uuid4(), email, f"U-{uuid4().hex[:8]}"),
            )
            create_user(conn, email, "a-password")
            conn.commit()
            session = login(conn, email, "a-password")
            conn.commit()
        yield client, session.token
