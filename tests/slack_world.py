"""A Slack workspace, two people, and a running Hippo.

The Slack surface needs more setup than the others: a connector row with a team
id, principals whose source ids are Slack user ids, content granted
asymmetrically, and a pending action to press a button on. Assembled here so
the tests read as three lines each.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from agent.providers.base import Completion, CompletionRequest, Usage
from api.main import create_app
from core.config import Settings
from core.db import Connection, connect
from resolver.embeddings import HashingEmbeddings

ORG_SCOPE = "00000000-0000-0000-0000-000000000001"
TEAM = "T-ACME"


class CitingModel:
    """Echoes the sources it was shown.

    Deliberately leaky: a model that repeated everything it was given is the
    worst case, and it is what makes "this answer contains only what that
    person can see" a real assertion rather than one about a model's manners.
    """

    name = "citing"
    model = "citing-1"

    def complete(self, request: CompletionRequest) -> Completion:
        shown = request.messages[0].content
        markers = " ".join(f"[{index}]" for index in range(1, shown.count("<source ") + 1))
        return Completion(
            text=f"{shown[-900:]} {markers}",
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@dataclass
class World:
    """Everything a Slack test needs to reach."""

    client: TestClient
    dsn: str
    connector_id: UUID
    principals: dict[str, UUID]

    def propose(self, slack_user: str) -> UUID:
        """A pending action belonging to one Slack user."""
        action_id = uuid4()
        with connect(self.dsn) as conn:
            entity = uuid4()
            conn.execute(
                "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'issue', 'ACME-1')",
                (entity,),
            )
            conn.execute(
                "INSERT INTO actions "
                "    (id, requested_by, action_type, target_entity, connector_id, payload, "
                "     risk_class, status, summary) "
                "VALUES (%s, %s, 'jira.comment', %s, %s, %s, 'consequential', 'pending', %s)",
                (
                    action_id,
                    self.principals[slack_user],
                    entity,
                    self.connector_id,
                    Jsonb({"body": "a summary"}),
                    "Comment on ACME-1",
                ),
            )
            conn.commit()
        return action_id

    def status_of(self, action_id: UUID) -> str:
        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM actions WHERE id = %s", (action_id,))
            row = cur.fetchone()
        return "" if row is None else str(row[0])

    def executed_at(self, action_id: UUID) -> datetime | None:
        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT executed_at FROM actions WHERE id = %s", (action_id,))
            row = cur.fetchone()
        return None if row is None else row[0]


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


class ProposingModel:
    """Answers every prompt with a proposal, so the action path is reachable."""

    name = "proposing"
    model = "proposing-1"

    def complete(self, request: CompletionRequest) -> Completion:
        return Completion(
            text='{"action_type": "jira.comment", "source": 1, "payload": {"body": "noted"}}',
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@contextmanager
def workspace(
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    model: object | None = None,
) -> Iterator[World]:
    """A running Hippo with a Slack connector and two linked people."""
    monkeypatch.setattr("api.main.build_provider", lambda _settings: model or CitingModel())
    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())

    settings = Settings(
        database_url=dsn,
        log_level="WARNING",
        service_name="hippo-test",
        slack_signing_secret=SecretStr(secret),
    )

    connector_id = uuid4()
    principals: dict[str, UUID] = {}

    # The client first: starting the app is what applies the migrations, so
    # seeding before it would be inserting into tables that do not exist yet.
    with TestClient(create_app(settings)) as client:
        with connect(dsn) as conn:
            conn.execute(
                "INSERT INTO connectors (id, kind, display_name, config) "
                "VALUES (%s, 'slack', 'Slack', %s)",
                (
                    connector_id,
                    Jsonb({"workspace_url": "https://acme.slack.com", "team_id": TEAM}),
                ),
            )
            for slack_user, note in (
                ("U-ALICE", "alice's private note about the liability cap on the renewal"),
                ("U-BOB", "bob's private note about the liability cap on the renewal"),
            ):
                principal = uuid4()
                conn.execute(
                    "INSERT INTO principals (id, kind, connector_id, source_id, email) "
                    "VALUES (%s, 'user', %s, %s, %s)",
                    (principal, connector_id, slack_user, f"{slack_user.lower()}@example.com"),
                )
                principals[slack_user] = principal
                _grant(conn, principal, note)
            # A Jira issue Alice can see, so a proposal has something real to
            # aim at: build_proposal() needs a connector behind the target.
            jira_id = uuid4()
            conn.execute(
                "INSERT INTO connectors (id, kind, display_name, config) "
                "VALUES (%s, 'jira', 'Jira', %s)",
                (jira_id, Jsonb({"base_url": "https://acme.atlassian.net"})),
            )
            issue = uuid4()
            conn.execute(
                "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'issue', 'ACME-1')",
                (issue,),
            )
            conn.execute(
                "INSERT INTO raw_records (connector_id, source_type, source_id, payload) "
                "VALUES (%s, 'jira.issue', 'ACME-1', '{}')",
                (jira_id,),
            )
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM raw_records WHERE connector_id = %s", (jira_id,))
                raw = (cur.fetchone() or (None,))[0]
            conn.execute(
                "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)",
                (issue, raw),
            )
            conn.execute(
                "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
                (issue, principals["U-ALICE"]),
            )
            conn.execute(
                "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
                "VALUES (%s, %s, 'ACME-1 the renewal is blocked on the liability cap', 0)",
                (issue, ORG_SCOPE),
            )
            # U-STRANGER is deliberately never created: the unlinked path is
            # about the principal mapping, not about the workspace.
            conn.commit()

        yield World(client=client, dsn=dsn, connector_id=connector_id, principals=principals)
