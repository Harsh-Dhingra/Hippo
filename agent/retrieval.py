"""Retrieval: planning a query and running it through the filter.

Every chunk the agent ever sees comes from `visible_chunks()` and from nowhere
else (CLAUDE.md rule 1). There is no second code path here to review, no
"fast path" that skips the filter, and no direct table read to fall back on —
the agent's database role could not execute one if there were.

Hybrid means three retrieval modes fused inside that one call: Postgres FTS,
pgvector, and graph expansion. Fusing them there rather than here is what keeps
the rule true; a client-side fusion would need to read the three result sets
separately, and two of those reads would have to come from somewhere else.

The planner is deterministic and calls no model. A model-driven planner is a
reasonable thing to want later, but it would add a round trip to every question
and it is not what makes the difference here: the case that decides whether
hybrid retrieval was worth building is an exact identifier like JIRA-123, and
recognising one is a regex, not a judgement call.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.db import Connection
from resolver.embeddings import EmbeddingProvider, to_pgvector

LOG = logging.getLogger("hippo.agent.retrieval")

# JIRA-123, ACME-7, C-DEALS. The exact-identifier case: a question naming one
# is asking about that specific thing, and pure vector search is the wrong tool
# for it — an identifier carries almost no semantic signal, so the nearest
# neighbours of "JIRA-123" are whatever else happens to look like an id.
IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{1,}-[A-Za-z0-9.]+\b")

# Questions about how things relate to each other are the ones worth walking
# the graph for. "What is blocking X" is not answered by the chunk that
# mentions X; it is answered by what X is connected to.
RELATIONAL = frozenset(
    {"blocking", "blocked", "blocker", "why", "cause", "related", "depends", "linked", "who"}
)

DEFAULT_K = 12


class RetrievalPlan(BaseModel):
    """What to ask the filter for.

    Stored on the answer so P1-AGT-4's trace can show why a query retrieved
    what it did, not just what came back.
    """

    model_config = ConfigDict(frozen=True)

    query_text: str
    use_vector: bool = True
    k: int = Field(default=DEFAULT_K, gt=0)
    hops: int = Field(default=1, ge=0, le=2)
    identifiers: tuple[str, ...] = ()

    @property
    def rationale(self) -> str:
        parts = ["keyword"]
        if self.use_vector:
            parts.append("vector")
        if self.hops:
            parts.append(f"{self.hops}-hop graph")
        reason = " + ".join(parts)
        if self.identifiers:
            reason += f"; exact identifiers {', '.join(self.identifiers)}"
        return reason


class Hit(BaseModel):
    """One retrieved chunk, as the filter returned it."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    entity_id: UUID
    entity_type: str
    entity_title: str | None
    content: str
    score: float
    retrieval_modes: tuple[str, ...]
    connector_id: UUID | None
    source_type: str | None
    source_id: str | None


def plan_query(question: str, *, k: int = DEFAULT_K) -> RetrievalPlan:
    """Decide how to search. Deterministic, and cheap enough to always run."""
    identifiers = tuple(dict.fromkeys(IDENTIFIER.findall(question)))
    words = {word.strip(".,?!'\"").lower() for word in question.split()}
    hops = 2 if words & RELATIONAL else 1

    return RetrievalPlan(
        query_text=question,
        # An identifier query still gets vector search: the filter fuses the
        # modes, so keeping it costs a little ranking noise and losing it would
        # drop every chunk that discusses the thing without naming it.
        use_vector=True,
        k=k,
        hops=hops,
        identifiers=identifiers,
    )


def retrieve(
    conn: Connection,
    principal_id: UUID,
    plan: RetrievalPlan,
    embedder: EmbeddingProvider | None = None,
) -> list[Hit]:
    """Run the plan through the permission filter.

    The embedder must be the one that indexed the corpus. Vectors from two
    different models are not comparable, and the failure is silent: retrieval
    returns confident nonsense rather than an error. That is the reason the
    agent and the resolver share one embeddings module instead of each
    configuring their own.
    """
    embedding: str | None = None
    if plan.use_vector and embedder is not None:
        (vector,) = embedder.embed([plan.query_text])
        embedding = to_pgvector(vector)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id, entity_id, entity_type, entity_title, content, score, "
            "       retrieval_modes, connector_id, source_type, source_id "
            "FROM visible_chunks(%s, %s, %s::vector, %s, %s)",
            (principal_id, plan.query_text, embedding, plan.k, plan.hops),
        )
        rows = cur.fetchall()

    hits = [_hit(row) for row in rows]
    LOG.info(
        "retrieved",
        extra={
            "principal_id": str(principal_id),
            "hits": len(hits),
            "plan": plan.rationale,
            "k": plan.k,
        },
    )
    return hits


def _hit(row: Any) -> Hit:
    return Hit(
        chunk_id=UUID(str(row[0])),
        entity_id=UUID(str(row[1])),
        entity_type=str(row[2]),
        entity_title=None if row[3] is None else str(row[3]),
        content=str(row[4]),
        score=float(row[5]),
        retrieval_modes=tuple(str(mode) for mode in (row[6] or ())),
        connector_id=None if row[7] is None else UUID(str(row[7])),
        source_type=None if row[8] is None else str(row[8]),
        source_id=None if row[9] is None else str(row[9]),
    )
