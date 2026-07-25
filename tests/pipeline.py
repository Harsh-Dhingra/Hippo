"""The resolver pipeline, as tests drive it.

Nothing here stands in for anything any more. Extraction, resolution and
enrichment are the real stages; this only saves each test from spelling out the
same three calls, and gives them a way to ask the permission filter a question.

The embedder is the offline hashing one, because CI must not call a model API
(CLAUDE.md, fixtures before live). Every other part of the path is what runs in
production.
"""

from __future__ import annotations

from uuid import UUID

from core.db import Connection
from resolver.embeddings import HashingEmbeddings
from resolver.enrichment import enrich_all
from resolver.resolution import resolve_connector
from resolver.summaries import ExtractiveSummarizer


def resolve_and_enrich(conn: Connection, connector_id: UUID | None = None) -> None:
    """Everything between raw records and a retrievable chunk."""
    resolve_connector(conn, connector_id)
    enrich_all(conn, HashingEmbeddings(), ExtractiveSummarizer(), connector_id)


def principal(conn: Connection, connector_id: UUID, source_id: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
            (connector_id, source_id),
        )
        row = cur.fetchone()
    assert row is not None, f"no principal for {source_id}"
    return UUID(str(row[0]))


def visible_text(conn: Connection, principal_id: UUID) -> set[str]:
    """What the permission filter will actually hand the model."""
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM visible_chunks(%s, NULL, NULL, 1000)", (principal_id,))
        return {str(row[0]) for row in cur.fetchall()}
