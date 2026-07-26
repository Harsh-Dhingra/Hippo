"""Running the golden set, and reporting numbers.

STACK.md makes this load-bearing: every graduation trigger in it is phrased as
a measurement, not a preference. "FTS relevance provably insufficient on the
eval harness, not on vibes." So this has to produce numbers a decision can rest
on, and the most important of them is per-mode recall — what keyword search
alone would have found, and what vector search alone would have found.

That number is free. A hit already carries which modes found it, so attributing
recall to a mode needs no second query and no separate index; it is the same
run, read differently.

Two floors, and they mean different things. `recall_at_k` is a quality bar that
should ratchet upward as retrieval improves, the way coverage does. `leaks` is
not a bar at all — it is zero, and a single one is a release-blocking bug
rather than a regression in a metric.

**Recall here is approximate, and the floors carry margin because of it.** The
vector index is HNSW (`chunks_embedding_idx`), which is an approximate nearest
neighbour structure: it trades exactness for speed, and the graph it searches
depends on the order rows went in. Rewriting the chunks table — a curation
pass, a re-embed, a restore — can therefore move recall a point or two without
anything in retrieval having changed.

The practical consequence is that a small delta between two runs is not
evidence of anything. Two numbers are only comparable if they came from the
same table state, which means an experiment that claims an improvement has to
apply and reverse the change over identical rows rather than compare a run
before a rewrite to a run after one. A measured example: curation appeared to
lift recall@20 from 0.756 to 0.789 until it was run as a controlled pair, at
which point the delta was 0.0000 at every k.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent.retrieval import Hit, plan_query, retrieve
from core.db import Connection
from evals.golden import Case
from resolver.embeddings import EmbeddingProvider, HashingEmbeddings

DEFAULT_K = 20


class Found(BaseModel):
    """One expected chunk, and how retrieval did on it."""

    model_config = ConfigDict(frozen=True)

    needle: str
    rank: int | None
    modes: tuple[str, ...] = ()

    @property
    def hit(self) -> bool:
        return self.rank is not None


class CaseResult(BaseModel):
    """What one labelled question produced."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    asker: str
    retrieved: int
    expected: tuple[Found, ...]
    leaked: tuple[str, ...]

    @property
    def recall(self) -> float:
        """1.0 when a case expects nothing, which is the honest reading.

        Three cases in the golden set expect nothing back, because the person
        asking is not entitled to an answer. Scoring those as zero recall would
        drag the headline number down for behaving correctly.
        """
        if not self.expected:
            return 1.0
        return sum(1 for item in self.expected if item.hit) / len(self.expected)

    @property
    def first_rank(self) -> int | None:
        ranks = [item.rank for item in self.expected if item.rank is not None]
        return min(ranks) if ranks else None


class Report(BaseModel):
    """The numbers."""

    model_config = ConfigDict(frozen=True)

    k: int
    results: tuple[CaseResult, ...]

    @property
    def recall_at_k(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.recall for result in self.results) / len(self.results)

    @property
    def mrr(self) -> float:
        """Mean reciprocal rank of the first expected chunk.

        Recall says whether the answer was in the set; this says whether it was
        near the top, which is what decides whether it survives a smaller k or
        a context budget.
        """
        scored = [result for result in self.results if result.expected]
        if not scored:
            return 0.0
        return sum(0.0 if r.first_rank is None else 1.0 / r.first_rank for r in scored) / len(
            scored
        )

    def recall_for(self, kind: str) -> float:
        """Recall over one family of case.

        The headline number hides the interesting one. Semantic cases are hard
        for a lexical embedder and easy for a real one; watching them
        separately is how a change of embedding model gets evaluated rather
        than asserted.
        """
        matching = [r for r in self.results if r.case_id.startswith(kind) and r.expected]
        if not matching:
            return 0.0
        return sum(result.recall for result in matching) / len(matching)

    @property
    def leaks(self) -> tuple[str, ...]:
        """Every forbidden chunk that came back. Not a metric — a bug list."""
        return tuple(
            f"{result.case_id} ({result.asker}): {needle}"
            for result in self.results
            for needle in result.leaked
        )

    def recall_by_mode(self) -> dict[str, float]:
        """What each retrieval mode would have found on its own.

        The number STACK.md's graduation triggers are written against. If
        keyword search alone stops finding things the fused ranking still
        finds, that is the measured case for a different index — and if it
        never does, the argument for one was never made.
        """
        wanted = [item for result in self.results for item in result.expected]
        if not wanted:
            return {}
        modes = {mode for item in wanted for mode in item.modes}
        return {
            mode: sum(1 for item in wanted if mode in item.modes) / len(wanted)
            for mode in sorted(modes)
        }

    def summary(self) -> str:
        lines = [
            f"cases          {len(self.results)}",
            f"k              {self.k}",
            f"recall@k       {self.recall_at_k:.3f}",
            f"MRR            {self.mrr:.3f}",
            f"leaks          {len(self.leaks)}",
        ]
        for mode, recall in self.recall_by_mode().items():
            lines.append(f"  by {mode:<10} {recall:.3f}")
        for kind in ("lexical", "semantic", "traversal"):
            lines.append(f"  {kind:<13} {self.recall_for(kind):.3f}")
        for result in self.results:
            missed = [item.needle for item in result.expected if not item.hit]
            if missed or result.leaked:
                lines.append(f"  {result.case_id}: missed={missed} leaked={list(result.leaked)}")
        return "\n".join(lines)


def principal_id(conn: Connection, source_id: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM principals WHERE source_id = %s LIMIT 1", (source_id,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no principal {source_id!r}; is the corpus seeded?")
    return UUID(str(row[0]))


def run_case(
    conn: Connection, case: Case, embedder: EmbeddingProvider, k: int = DEFAULT_K
) -> CaseResult:
    """Retrieve for one case, exactly as the agent would.

    Through `retrieve()` rather than by querying the filter directly, so the
    thing measured is the path that actually runs — including the planner's
    choice of hops, which is part of what is being evaluated.
    """
    hits = retrieve(conn, principal_id(conn, case.asker), plan_query(case.question, k=k), embedder)

    return CaseResult(
        case_id=case.id,
        asker=case.asker,
        retrieved=len(hits),
        expected=tuple(_locate(needle, hits) for needle in case.must_retrieve),
        leaked=tuple(
            needle
            for needle in case.must_not_retrieve
            if any(needle in hit.content for hit in hits)
        ),
    )


def _locate(needle: str, hits: Sequence[Hit]) -> Found:
    for rank, hit in enumerate(hits, start=1):
        if needle in hit.content:
            return Found(needle=needle, rank=rank, modes=hit.retrieval_modes)
    return Found(needle=needle, rank=None)


def cases_from(corpus: object) -> tuple[Case, ...]:
    """Derive golden cases from what the generator planted.

    Derived rather than written down, so the labels cannot drift from the
    corpus. A hand-maintained golden set over a generated graph is two things
    that have to agree, and they stop agreeing the first time the generator
    changes.
    """
    from evals.seed import Corpus

    assert isinstance(corpus, Corpus)
    everyone = "U-EVAL-01"
    cases: list[Case] = []

    for index, fact in enumerate(corpus.facts):
        roster = corpus.members[fact.channel]
        # Ask as someone who can see it, so the case measures retrieval.
        cases.append(
            Case(
                id=f"{fact.kind}-{index}",
                question=fact.question,
                asker=roster[0],
                # The whole answer, never a prefix. Six paraphrases are reused
                # across twenty-four channels, so a truncated needle matches a
                # public twin of a private fact and reads as a leak. Every
                # planted answer ends with its channel, which is what makes the
                # full string unique.
                must_retrieve=(fact.answer,),
                tests=f"{fact.kind} retrieval",
            )
        )
        # And, for private facts, as someone who cannot: the same question,
        # the opposite expectation.
        if fact.private and everyone not in roster:
            cases.append(
                Case(
                    id=f"{fact.kind}-{index}-denied",
                    question=fact.question,
                    asker=everyone,
                    must_retrieve=(),
                    must_not_retrieve=(fact.answer,),
                    tests=f"{fact.kind} retrieval, from outside the channel",
                )
            )

    return tuple(cases)


def run(
    conn: Connection,
    cases: Sequence[Case],
    embedder: EmbeddingProvider | None = None,
    k: int = DEFAULT_K,
) -> Report:
    """Run the whole set.

    The offline embedder by default. It is lexical rather than semantic, so the
    vector numbers here are a floor and not a forecast — which is the honest
    position until P1-RES-3 picks a model, and is why the paraphrase case in
    the golden set is worth watching when one is chosen.
    """
    resolved = embedder if embedder is not None else HashingEmbeddings()
    return Report(k=k, results=tuple(run_case(conn, case, resolved, k) for case in cases))
