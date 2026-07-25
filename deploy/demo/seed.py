"""Seed a database with the demo world, for driving the UI.

The same fixture corpus the tests use, plus two users and one pending action —
so every screen the §12 demo walks through has something real on it. Not a
test: a way to bring the product up on a laptop with no Slack workspace and no
Jira site, which is also what a first-time reader of the README needs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from agent.links import load_directory
from agent.loop import Agent
from agent.providers.base import Completion, CompletionRequest, Usage
from api import auth
from core.db import Connection, connect
from core.migrate import upgrade
from resolver.embeddings import HashingEmbeddings
from resolver.enrichment import enrich_all
from resolver.resolution import link_principal_identities, resolve_connector
from resolver.summaries import ExtractiveSummarizer
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
PASSWORD = "hippo-demo-password"


def main(dsn: str) -> None:
    with connect(dsn, autocommit=True) as conn:
        upgrade(conn)

    with connect(dsn) as conn:
        slack_id, jira_id = uuid4(), uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connectors (id, kind, display_name, config) VALUES "
                "(%s, 'slack', 'Acme Slack', %s), (%s, 'jira', 'Acme Jira', %s)",
                (
                    slack_id,
                    json.dumps({"workspace_url": "https://acme.slack.com"}),
                    jira_id,
                    json.dumps({"base_url": "https://acme.atlassian.net"}),
                ),
            )

        SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(conn)
        SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(conn)
        resolve_connector(conn)
        link_principal_identities(conn)
        enrich_all(conn, HashingEmbeddings(), ExtractiveSummarizer())
        # ACLs again, because the projection reads entities and the resolver is
        # what creates them. In production this is the next tick of the fast lane.
        SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_stream(
            conn, "acls"
        )
        SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_stream(
            conn, "acls"
        )

        for email, name in (("alice@example.com", "Alice"), ("carol@example.com", "Carol")):
            auth.create_user(conn, email, PASSWORD, display_name=name)

        _seed_pending_action(conn, jira_id)
        _seed_traces(conn, slack_id)
        conn.commit()

    print(f"seeded. sign in as alice@example.com or carol@example.com / {PASSWORD}")


def _seed_pending_action(conn: Connection, jira_id: UUID) -> None:
    """One proposal waiting for a human, so the approvals screen is not empty.

    Written directly rather than by asking the agent, because seeding must not
    need a model API key.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, e.id FROM principals p, entities e "
            "WHERE p.source_id = 'U-ALICE' AND e.title = 'Acme renewal blocked on legal review' "
            "LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            print("no target for a demo action; skipping")
            return
        cur.execute(
            "INSERT INTO actions (requested_by, connector_id, action_type, target_entity, "
            "    payload, risk_class, status) "
            "VALUES (%s, %s, 'jira.comment', %s, %s, 'consequential', 'pending')",
            (
                row[0],
                jira_id,
                row[1],
                json.dumps({"body": "Legal review is the blocker; engineering is done."}),
            ),
        )


class QuotingModel:
    """A model that answers by citing whatever it was given.

    Local, deterministic and free. Seeding must not need an API key, and the
    trace view needs a real trace to show: real retrieval through the real
    permission filter, with a real prompt recorded. Only the wording of the
    answer is fake.
    """

    name = "demo"
    model = "demo-quoting-model"

    def complete(self, request: CompletionRequest) -> Completion:
        sources = request.messages[0].content.count("<source ")
        markers = " ".join(f"[{index}]" for index in range(1, min(sources, 3) + 1))
        return Completion(
            text=(
                "Legal review is the blocker: engineering is done and the "
                f"renewal ticket is waiting on the liability clause. {markers}"
            ),
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=420, output_tokens=38),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return sum(len(message.content) for message in request.messages) // 4


def _seed_traces(conn: Connection, slack_id: UUID) -> None:
    """One real query per user, so the trace view has something to show.

    Asked as both Alice and Carol on purpose: side by side, the two traces are
    the filtered-path demo. Same question, same plan, different retrieval
    lists — and Carol's is short because the filter returned little, not
    because anything was removed afterwards.
    """
    agent = Agent(QuotingModel(), embedder=HashingEmbeddings(), directory=load_directory(conn))
    question = "What is blocking the Acme renewal?"

    for source_id in ("U-ALICE", "U-CAROL"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
                (slack_id, source_id),
            )
            row = cur.fetchone()
        if row is None:
            continue
        answer = agent.answer(conn, UUID(str(row[0])), question, k=20)
        print(f"  {source_id}: {len(answer.hits)} sources visible")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "postgresql://localhost:5432/hippo_demo")
