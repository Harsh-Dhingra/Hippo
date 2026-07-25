"""Enrichment: chunks, embeddings, summaries.

The third resolver stage, and the one whose whole design question is what a
re-run costs. Chunking policies change, embedding models get swapped, summaries
need regenerating: STACK.md treats re-embedding as a resolver re-run by design,
so a re-run has to be cheap enough to be a normal operation.

It is cheap because a chunk is identified by the hash of its content. A re-run
recomputes the same hashes for text that has not changed, leaves those rows
alone, and they keep the embedding they already have. Only text that genuinely
changed costs an embedding call, and text that disappeared is deleted rather
than left behind for retrieval to find.

Summaries are the other half of the same idea. They are regenerated from the
chunks that exist now and overwrite what was there, so a stale summary cannot
survive a re-run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from prometheus_client import Counter

from core.db import Connection
from resolver.chunking import Chunk, chunks_for
from resolver.embeddings import EmbeddingProvider, to_pgvector
from resolver.summaries import Summarizer

LOG = logging.getLogger("hippo.resolver.enrichment")

# Every chunk lands in the org scope for now. Team and personal scopes are
# P2-MEM-1's surface; the column and the filter already understand them.
ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

CHUNKS_WRITTEN = Counter("hippo_resolver_chunks_written_total", "Chunks created.")
CHUNKS_REMOVED = Counter(
    "hippo_resolver_chunks_removed_total", "Chunks deleted because their text is gone."
)
CHUNKS_EMBEDDED = Counter("hippo_resolver_chunks_embedded_total", "Chunks sent for embedding.")


@dataclass
class EnrichmentStats:
    """What one enrichment pass did."""

    entities: int = 0
    chunks_created: int = 0
    chunks_removed: int = 0
    chunks_embedded: int = 0
    summaries_written: int = 0

    @property
    def unchanged(self) -> bool:
        return not (self.chunks_created or self.chunks_removed or self.summaries_written)


def _source_payloads(conn: Connection, entity_id: UUID) -> list[tuple[str, dict[str, object]]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.source_type, r.payload FROM entity_sources es "
            "JOIN raw_records r ON r.id = es.raw_record_id "
            "WHERE es.entity_id = %s ORDER BY r.source_type, r.source_id",
            (entity_id,),
        )
        return [(str(row[0]), dict(row[1])) for row in cur.fetchall()]


def desired_chunks(conn: Connection, entity_id: UUID) -> list[Chunk]:
    """What this entity's chunks should be, from its raw records.

    Deduplicated by content: the unique index would reject the second copy
    anyway, and doing it here keeps the index count honest.
    """
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for source_type, payload in _source_payloads(conn, entity_id):
        for chunk in chunks_for(source_type, payload, start_index=len(chunks)):
            if chunk.content_hash in seen:
                continue
            seen.add(chunk.content_hash)
            chunks.append(chunk)
    return chunks


def enrich_entity(
    conn: Connection,
    entity_id: UUID,
    provider: EmbeddingProvider,
    summarizer: Summarizer,
    stats: EnrichmentStats,
) -> None:
    """Bring one entity's chunks, embeddings and summary up to date."""
    wanted = desired_chunks(conn, entity_id)
    by_hash = {chunk.content_hash: chunk for chunk in wanted}

    with conn.cursor() as cur:
        cur.execute("SELECT content_hash FROM chunks WHERE entity_id = %s", (entity_id,))
        existing = {str(row[0]) for row in cur.fetchall()}

    stale = existing - set(by_hash)
    if stale:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE entity_id = %s AND content_hash = ANY(%s)",
                (entity_id, sorted(stale)),
            )
        stats.chunks_removed += len(stale)
        CHUNKS_REMOVED.inc(len(stale))

    for chunk in wanted:
        if chunk.content_hash in existing:
            # Unchanged text keeps its row and therefore its embedding. This is
            # the line that makes a re-run affordable.
            continue
        with conn.cursor() as cur:
            # content_hash is set by a trigger, so it is deliberately not
            # passed here: one place computes it, and the ON CONFLICT below
            # still sees it because the trigger runs first.
            cur.execute(
                "INSERT INTO chunks (entity_id, scope_id, content, chunk_index, token_count) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (entity_id, content_hash) DO NOTHING",
                (entity_id, ORG_SCOPE, chunk.content, chunk.index, chunk.token_estimate),
            )
        stats.chunks_created += 1
        CHUNKS_WRITTEN.inc()

    _embed_pending(conn, entity_id, provider, stats)
    _write_summary(conn, entity_id, wanted, summarizer, stats)
    stats.entities += 1


def _embed_pending(
    conn: Connection, entity_id: UUID, provider: EmbeddingProvider, stats: EnrichmentStats
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, content FROM chunks "
            "WHERE entity_id = %s AND embedding IS NULL ORDER BY chunk_index",
            (entity_id,),
        )
        pending = [(UUID(str(row[0])), str(row[1])) for row in cur.fetchall()]

    if not pending:
        return

    vectors = provider.embed([content for _id, content in pending])
    for (chunk_id, _content), vector in zip(pending, vectors, strict=True):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chunks SET embedding = %s::vector WHERE id = %s",
                (to_pgvector(vector), chunk_id),
            )
    stats.chunks_embedded += len(pending)
    CHUNKS_EMBEDDED.inc(len(pending))


def _write_summary(
    conn: Connection,
    entity_id: UUID,
    chunks: list[Chunk],
    summarizer: Summarizer,
    stats: EnrichmentStats,
) -> None:
    """Replace, never append. A summary is derived from what exists now."""
    with conn.cursor() as cur:
        cur.execute("SELECT title, summary FROM entities WHERE id = %s", (entity_id,))
        row = cur.fetchone()
    if row is None:
        return

    title, previous = str(row[0]) if row[0] is not None else None, row[1]
    summary = summarizer.summarize(title, [chunk.content for chunk in chunks])
    if summary == previous:
        return

    with conn.cursor() as cur:
        cur.execute("UPDATE entities SET summary = %s WHERE id = %s", (summary, entity_id))
    stats.summaries_written += 1


def enrichable_entities(conn: Connection, connector_id: UUID | None = None) -> list[UUID]:
    """Entities backed by a source type that has a chunking policy."""
    sql = (
        "SELECT DISTINCT es.entity_id FROM entity_sources es "
        "JOIN raw_records r ON r.id = es.raw_record_id "
        "WHERE r.source_type = ANY(%s)"
    )
    params: list[object] = [sorted(_policy_types())]
    if connector_id is not None:
        sql += " AND r.connector_id = %s"
        params.append(connector_id)
    sql += " ORDER BY 1"

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [UUID(str(row[0])) for row in cur.fetchall()]


def _policy_types() -> list[str]:
    from resolver.chunking import POLICIES

    return list(POLICIES)


def enrich_all(
    conn: Connection,
    provider: EmbeddingProvider,
    summarizer: Summarizer,
    connector_id: UUID | None = None,
) -> EnrichmentStats:
    """Enrich everything with a chunking policy. Safe to run repeatedly."""
    stats = EnrichmentStats()
    for entity_id in enrichable_entities(conn, connector_id):
        enrich_entity(conn, entity_id, provider, summarizer, stats)

    LOG.info(
        "enrichment pass complete",
        extra={
            "entities": stats.entities,
            "chunks_created": stats.chunks_created,
            "chunks_removed": stats.chunks_removed,
            "chunks_embedded": stats.chunks_embedded,
            "summaries_written": stats.summaries_written,
            "embedding_model": provider.model,
            "summarizer": summarizer.name,
        },
    )
    return stats
