"""A stand-in for P1-RES-3, so ACL tests can reach the permission filter.

The filter returns chunks, and chunking policy is enrichment's job. This makes
one chunk per text-bearing entity from the title extraction already derived,
which is enough to ask what a given person can see.

Everything above it is real: P1-RES-1 extracts, P1-RES-2 resolves, and the ACL
projection runs against the entities those stages actually produced.
"""

from __future__ import annotations

from uuid import UUID

from core.db import Connection

ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Entity types whose title is the content someone would want to find.
CHUNKABLE = ("message", "comment", "ticket")


def chunk_everything(conn: Connection) -> int:
    """One chunk per text-bearing entity. Returns how many were made."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chunks (entity_id, scope_id, content) "
            "SELECT e.id, %s, e.title FROM entities e "
            "WHERE e.entity_type = ANY(%s) AND e.title IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.entity_id = e.id)",
            (ORG_SCOPE, list(CHUNKABLE)),
        )
        return cur.rowcount


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
