"""A stand-in for P1-RES-1, so ACL tests can reach the permission filter.

The ACL projection needs entities to attach grants to, and entities are the
resolver's job. Until P1-RES-1 exists, this does the minimum the projection
depends on: one entity per raw record, linked through entity_sources, plus a
chunk for anything with text so the filter has something to return.

Everything the real resolver adds on top (identity merging, summaries, edges)
is irrelevant to whether a grant lands on the right entity, which is what the
tests using this are asking about.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core.db import Connection

ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Source types whose payload carries text worth chunking.
CHUNKABLE = ("message", "comment", "issue")


def _text_of(payload: dict[str, Any]) -> str | None:
    for key in ("text", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    fields = payload.get("fields")
    if isinstance(fields, dict):
        summary = fields.get("summary")
        if isinstance(summary, str) and summary:
            return summary
    return None


def resolve_like_the_resolver(conn: Connection, connector_id: UUID) -> None:
    """Create one entity per raw record, and a chunk per textual record."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_type, source_id, payload FROM raw_records "
            "WHERE connector_id = %s ORDER BY source_type, source_id",
            (connector_id,),
        )
        rows = cur.fetchall()

    for raw_id, source_type, source_id, payload in rows:
        entity_type = str(source_type).rsplit(".", 1)[-1]
        text = _text_of(payload)
        title = payload.get("name") or text or str(source_id)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO entities (entity_type, title) VALUES (%s, %s) RETURNING id",
                (entity_type, str(title)[:200]),
            )
            row = cur.fetchone()
            assert row is not None
            entity_id = row[0]
            cur.execute(
                "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)",
                (entity_id, raw_id),
            )
            if entity_type in CHUNKABLE and text:
                cur.execute(
                    "INSERT INTO chunks (entity_id, scope_id, content) VALUES (%s, %s, %s)",
                    (entity_id, ORG_SCOPE, text),
                )


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
