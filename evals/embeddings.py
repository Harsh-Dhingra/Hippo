"""Choosing an embedding model by measuring, not by reputation.

STACK.md defers the pick: "Config-abstracted; default: a current strong
open-weights model, 1024-dim (final pick at P1-RES-3 with a small eval)". This
is that eval. It exists because a guessed default ships an unmeasured decision
to every adopter, and because "which embedding model" is the single choice that
moves retrieval quality most.

The method is the only honest one available: embed the same corpus with each
candidate, ask the same questions, and read the same numbers. Nothing here
consults a leaderboard. A model that scores well on MTEB and badly on a
permission-filtered corpus of short Slack messages is the wrong model for this
project, and the only way to find that out is to run it.

**Dimension is a hard constraint, not a preference.** `chunks.embedding` is
`vector(1024)`, so a 768-dimension model is not a slightly different choice —
it is a migration and a full re-embed. Candidates are 1024-dimension unless
someone has a measured reason to pay that.

    python -m evals.embeddings                    # every candidate
    python -m evals.embeddings mxbai-embed-large  # just one
"""

from __future__ import annotations

import sys
import time

from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict

from core.db import connect
from core.migrate import upgrade
from evals.harness import cases_from, run
from evals.scratch import admin_dsn, scratch_database
from evals.seed import build
from resolver.embeddings import (
    EmbeddingProvider,
    HashingEmbeddings,
    OpenAICompatibleEmbeddings,
)

# Anything speaking the OpenAI /embeddings shape. Ollama does, which is what
# makes this runnable with no API key and no account — the same property
# STACK.md wants from the provider interface generally.
LOCAL = "http://localhost:11434/v1"


class Candidate(BaseModel):
    """One model worth measuring."""

    model_config = ConfigDict(frozen=True)

    name: str
    dimensions: int
    note: str

    def build(self) -> EmbeddingProvider:
        if self.name == "hashing":
            return HashingEmbeddings()
        return OpenAICompatibleEmbeddings(
            self.name, base_url=LOCAL, dimensions=self.dimensions, batch_size=32
        )


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        name="hashing",
        dimensions=1024,
        note="the offline default; lexical, and the baseline to beat",
    ),
    Candidate(
        name="mxbai-embed-large",
        dimensions=1024,
        note="open weights, 1024-dim, no dimension change needed",
    ),
    Candidate(
        name="bge-m3",
        dimensions=1024,
        note="open weights, 1024-dim, multilingual",
    ),
)


class Measurement(BaseModel):
    """What one candidate scored."""

    model_config = ConfigDict(frozen=True)

    candidate: str
    recall: float
    mrr: float
    lexical: float
    semantic: float
    traversal: float
    vector_attribution: float
    leaks: int
    embed_seconds: float

    def row(self) -> str:
        return (
            f"{self.candidate:<22} {self.recall:.3f}  {self.mrr:.3f}  "
            f"{self.lexical:.2f}  {self.semantic:.2f}  {self.traversal:.2f}  "
            f"{self.vector_attribution:.3f}  {self.leaks:>5}  {self.embed_seconds:6.1f}s"
        )


def measure(candidate: Candidate, k: int = 20) -> Measurement:
    """Seed a fresh corpus with this model and run the golden set against it.

    A fresh database per candidate, because the embeddings are the variable
    under test and re-embedding in place would leave the question of whether
    anything else moved.
    """
    dsn = scratch_database("hippo_embed")

    try:
        with connect(dsn, autocommit=True) as conn:
            upgrade(conn)
        with connect(dsn) as conn:
            started = time.monotonic()
            corpus = build(conn, embedder=candidate.build())
            elapsed = time.monotonic() - started
            conn.commit()
            report = run(conn, cases_from(corpus), embedder=candidate.build(), k=k)

        return Measurement(
            candidate=candidate.name,
            recall=report.recall_at_k,
            mrr=report.mrr,
            lexical=report.recall_for("lexical"),
            semantic=report.recall_for("semantic"),
            traversal=report.recall_for("traversal"),
            vector_attribution=report.recall_by_mode().get("vector", 0.0),
            leaks=len(report.leaks),
            embed_seconds=elapsed,
        )
    finally:
        # Unlike the other two, this one cleans up after itself: it builds a
        # database per candidate, so leaving them behind means a pile of them.
        with connect(admin_dsn(), autocommit=True) as conn:
            conn.execute(
                f'DROP DATABASE IF EXISTS "{conninfo_to_dict(dsn)["dbname"]}" WITH (FORCE)'
            )


def main(only: str | None = None) -> int:
    chosen = [c for c in CANDIDATES if only is None or c.name == only]
    if not chosen:
        print(f"no candidate named {only!r}; known: {[c.name for c in CANDIDATES]}")
        return 2

    print(f"{'model':<22} {'recall':>6}  {'mrr':>5}  lex   sem   trav  vector  leaks   embed")
    print("-" * 88)

    results: list[Measurement] = []
    for candidate in chosen:
        try:
            measurement = measure(candidate)
        except Exception as exc:  # a model that will not run is a real result
            print(f"{candidate.name:<22} unavailable: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        results.append(measurement)
        print(measurement.row())

    if not results:
        return 1

    # Leaks are not a tiebreak. A model that leaked would be disqualified
    # outright, and saying so here is cheaper than discovering the assumption
    # later.
    leaking = [m for m in results if m.leaks]
    if leaking:
        print("\nDISQUALIFIED — a candidate leaked:")
        for measurement in leaking:
            print(f"  {measurement.candidate}: {measurement.leaks}")
        return 1

    best = max(results, key=lambda m: (m.semantic, m.recall))
    print(f"\nbest on semantic recall: {best.candidate} ({best.semantic:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
