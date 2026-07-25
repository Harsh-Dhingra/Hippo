"""Trying, deliberately, to make the filter hand over something it should not.

This is not the permission property test again. That one proves the *filter*
agrees with its specification across ten thousand random ACL worlds, which is a
statement about SQL. This proves that *retrieval as the agent actually invokes
it* never surfaces forbidden content, whatever shape the query takes — which is
a statement about the code path, and the two can come apart.

They would come apart if a future change let a parameter widen the result set:
a k large enough to fall back to a scan, a hop count that walked an edge into
something ungranted, a query mode that skipped a join. None of those are
hypothetical failure shapes; they are the ordinary ways a retrieval layer
regresses. Each gets a probe here.

The probes are built *from the forbidden content itself*, which is the strongest
form of the test. An attacker who has seen a private message elsewhere and is
now fishing for it through Hippo is not guessing at phrasings — they are
quoting. If verbatim text of a chunk you cannot see does not retrieve it,
nothing weaker will.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from pydantic import BaseModel, ConfigDict

from agent.retrieval import RetrievalPlan, retrieve
from core.db import Connection
from evals.harness import principal_id
from resolver.embeddings import EmbeddingProvider, HashingEmbeddings

MAX_K = 100
MAX_HOPS = 2


class Probe(BaseModel):
    """One attempt, and why it is worth attempting."""

    model_config = ConfigDict(frozen=True)

    label: str
    plan: RetrievalPlan


class Leak(BaseModel):
    """A probe that returned something it should not have. Always a bug."""

    model_config = ConfigDict(frozen=True)

    asker: str
    probe: str
    forbidden: str
    content: str


def probes_for(secret: str) -> Iterator[Probe]:
    """Every way of asking for one forbidden string that we can think of."""
    words = re.findall(r"[A-Za-z0-9]{4,}", secret)

    # Verbatim. The strongest probe there is: someone who already knows the
    # text and is checking whether Hippo will confirm it.
    yield Probe(label="verbatim", plan=RetrievalPlan(query_text=secret, k=MAX_K, hops=MAX_HOPS))

    # Every distinctive word on its own, in case fusion behaves differently for
    # a one-term query than for a sentence.
    for word in words[:8]:
        yield Probe(
            label=f"single-word:{word}",
            plan=RetrievalPlan(query_text=word, k=MAX_K, hops=MAX_HOPS),
        )

    # The words rearranged, so nothing can be passing because of phrase order.
    if len(words) > 2:
        yield Probe(
            label="shuffled",
            plan=RetrievalPlan(query_text=" ".join(reversed(words)), k=MAX_K, hops=MAX_HOPS),
        )

    # No vector, so keyword search is unassisted and unfused.
    yield Probe(
        label="keyword-only",
        plan=RetrievalPlan(query_text=secret, use_vector=False, k=MAX_K, hops=MAX_HOPS),
    )

    # Maximum graph expansion. The forbidden chunk is one hop from things the
    # asker *can* see — the same channel, the same project — so this is the
    # probe most likely to find a real hole.
    yield Probe(
        label="max-hops",
        plan=RetrievalPlan(query_text=secret, k=MAX_K, hops=MAX_HOPS),
    )

    # No hops at all, in case expansion is what constrains rather than widens.
    yield Probe(
        label="no-hops",
        plan=RetrievalPlan(query_text=secret, k=MAX_K, hops=0),
    )


def enumeration_probes() -> Iterator[Probe]:
    """Attempts to get the whole corpus rather than one secret.

    Browse mode returns everything visible, so a filter bug shows up here as a
    count rather than as a match, and a huge k is the natural way to reach for
    "everything you have".
    """
    yield Probe(label="browse-everything", plan=RetrievalPlan(query_text="", k=MAX_K, hops=0))
    yield Probe(label="browse-with-hops", plan=RetrievalPlan(query_text="", k=MAX_K, hops=MAX_HOPS))
    for term in ("the", "a", "and", "renewal", "acme", "%", "*", "'"):
        yield Probe(
            label=f"broad:{term}",
            plan=RetrievalPlan(query_text=term, k=MAX_K, hops=MAX_HOPS),
        )


def hunt(
    conn: Connection,
    asker: str,
    forbidden: Sequence[str],
    embedder: EmbeddingProvider | None = None,
) -> list[Leak]:
    """Run every probe as one principal, and collect what came back wrongly."""
    resolved = embedder if embedder is not None else HashingEmbeddings()
    principal = principal_id(conn, asker)
    leaks: list[Leak] = []

    def check(probe: Probe) -> None:
        for hit in retrieve(conn, principal, probe.plan, resolved):
            for secret in forbidden:
                if secret in hit.content:
                    leaks.append(
                        Leak(
                            asker=asker,
                            probe=probe.label,
                            forbidden=secret,
                            content=hit.content[:120],
                        )
                    )

    for secret in forbidden:
        for probe in probes_for(secret):
            check(probe)
    for probe in enumeration_probes():
        check(probe)

    return leaks


def probe_count(forbidden: Sequence[str]) -> int:
    """How many attempts a hunt makes. Reported so that a suite which quietly
    stopped probing is visible as a number falling, rather than as a green
    tick."""
    return sum(1 for secret in forbidden for _ in probes_for(secret)) + sum(
        1 for _ in enumeration_probes()
    )
