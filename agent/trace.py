"""The per-query trace.

ARCHITECTURE §9 calls this a feature rather than debug output, and it is worth
being precise about why. Three separate claims this project makes are only
checkable from here:

- *The answer is grounded.* The trace lists every chunk retrieved, in rank
  order, with which retrieval mode found it and whether the answer cited it.
- *You did not see what you cannot see.* The trace of a filtered query shows a
  short retrieval list, not a redacted one. What was excluded never appears
  because it was never returned, and the plan beside it shows the query was not
  narrowed to hide anything.
- *The only egress is the prompt.* The system prompt is stored verbatim and the
  fenced sources are stored as chunk ids with the content hash they had at the
  time, so the exact prompt can be rebuilt and the rebuild can be verified.

Storing hashes rather than content is what keeps the trace cheap (§9 again),
and it has a second effect worth naming: a trace never becomes a second copy of
the corpus sitting outside the permission filter.

Recording never fails a query. A trace is a record of something that already
happened, and losing the record is worse than losing nothing but much better
than losing the answer — so `record()` logs and swallows.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from core.db import Connection

LOG = logging.getLogger("hippo.agent.trace")


class Step(BaseModel):
    """One node of the graph, and how long it took."""

    model_config = ConfigDict(frozen=True)

    name: str
    duration_ms: int
    detail: dict[str, Any] = Field(default_factory=dict)


class StepTimer:
    """Collects steps as the graph runs.

    Wall-clock from a monotonic source: a trace that reported a negative
    duration because the clock moved would undermine the one thing it is for.
    """

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self._start = time.monotonic()

    def step(self, name: str, started: float, **detail: Any) -> None:
        self.steps.append(
            Step(
                name=name,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=detail,
            )
        )

    @property
    def total_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


class TraceRetrieval(BaseModel):
    """One retrieved chunk, as the trace remembers it. No content."""

    model_config = ConfigDict(frozen=True)

    rank: int
    chunk_id: UUID
    entity_id: UUID
    entity_type: str | None
    entity_title: str | None
    content_hash: str | None
    score: float
    retrieval_modes: tuple[str, ...]
    cited: bool


class Trace(BaseModel):
    """Everything one query did."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    question: str
    plan: dict[str, Any]
    route: str
    steps: tuple[Step, ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    answer: str | None = None
    citations: tuple[UUID, ...] = ()
    refused: bool = False
    action_id: UUID | None = None
    error: str | None = None
    duration_ms: int = 0
    retrievals: tuple[TraceRetrieval, ...] = ()


def record(conn: Connection, principal_id: UUID, trace: Trace) -> UUID | None:
    """Write the trace. Never raises.

    A failure here means the query is unauditable, which is worth a loud log
    and is not worth failing an answer the user already has.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO query_traces "
                "  (id, principal_id, question, plan, route, steps, system_prompt, model, "
                "   provider, input_tokens, output_tokens, answer, citations, refused, "
                "   action_id, error, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    trace.id,
                    principal_id,
                    trace.question,
                    Jsonb(trace.plan),
                    trace.route,
                    Jsonb([step.model_dump() for step in trace.steps]),
                    trace.system_prompt,
                    trace.model,
                    trace.provider,
                    trace.input_tokens,
                    trace.output_tokens,
                    trace.answer,
                    list(trace.citations),
                    trace.refused,
                    trace.action_id,
                    trace.error,
                    trace.duration_ms,
                ),
            )
            if trace.retrievals:
                cur.executemany(
                    "INSERT INTO trace_retrievals "
                    "  (trace_id, rank, chunk_id, entity_id, entity_type, entity_title, "
                    "   content_hash, score, retrieval_modes, cited) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            trace.id,
                            item.rank,
                            item.chunk_id,
                            item.entity_id,
                            item.entity_type,
                            item.entity_title,
                            item.content_hash,
                            item.score,
                            list(item.retrieval_modes),
                            item.cited,
                        )
                        for item in trace.retrievals
                    ],
                )
    except Exception as exc:
        LOG.error("could not record the trace", extra={"error": str(exc), "trace": str(trace.id)})
        return None

    LOG.info(
        "trace recorded",
        extra={
            "trace_id": str(trace.id),
            "route": trace.route,
            "retrieved": len(trace.retrievals),
            "duration_ms": trace.duration_ms,
        },
    )
    return trace.id


def content_hash(text: str) -> str:
    """The hash migration 006's trigger gives that chunk.

    Computed here rather than returned by the filter: it is a pure function of
    content the agent already holds, and adding an output column to the one
    granted function to carry a value that can be derived is the wrong kind of
    change to make to that function.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_trace_id() -> UUID:
    """Allocated before the query runs, so a failure still has an id to file
    itself under."""
    return uuid4()


def list_traces(conn: Connection, principal_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    """This principal's recent queries. Someone else's are not reachable."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM my_traces(%s, %s)", (principal_id, limit))
        columns = [description.name for description in cur.description or []]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def load_trace(conn: Connection, principal_id: UUID, trace_id: UUID) -> dict[str, Any] | None:
    """One trace in full, or None.

    None covers both "no such trace" and "not yours", and deliberately does not
    distinguish them: a different answer for the second case would confirm that
    a trace exists.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM my_trace(%s, %s)", (principal_id, trace_id))
        columns = [description.name for description in cur.description or []]
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row, strict=True))
