"""Running somebody else's benchmark.

PROJECT.md asks for results against published enterprise-agent benchmarks "if
reproducible". This is the half of that which can be built without one in hand:
a loader for a documented format, so adopting a benchmark is writing one file
rather than starting a project.

**Most published benchmarks cannot measure what this system claims.** They
supply documents and questions and score the answer. Hippo's central claim is
not that it answers well — it is that it answers *only from what the person
asking is allowed to read*, and a corpus with no permission model cannot tell
you whether that holds. Run one of those here and you have measured the
retrieval half honestly and the half that matters not at all.

So the format below makes ACLs a first-class field, and the runner reports two
numbers side by side: recall, which any benchmark can score, and leaks, which
only a benchmark with a permission model can. A benchmark without one reports
`leaks = not measured` rather than `leaks = 0`, because zero out of zero
possible leaks is not a result.

    python -m evals.external path/to/benchmark.json
    python -m evals.external path/to/benchmark.json postgresql://…

The format is deliberately small — documents, people, questions:

    {
      "name": "some-published-benchmark",
      "source": "https://…",
      "people": [{"id": "u1", "email": "a@x.com", "groups": ["eng"]}],
      "documents": [
        {"id": "d1", "title": "…", "text": "…",
         "readable_by": ["u1", "eng"], "occurred_at": "2026-01-01T00:00:00Z"}
      ],
      "questions": [
        {"id": "q1", "asker": "u1", "question": "…",
         "answer_in": ["d1"], "must_not_see": ["d2"]}
      ]
    }

`readable_by` may name a person or a group. Omitting it entirely means the
document is readable by everyone, which is what a benchmark with no permission
model amounts to — and the runner says so rather than scoring it as safe.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent.retrieval import plan_query, retrieve
from core.db import Connection, connect
from core.migrate import upgrade
from evals.scratch import scratch_database
from resolver.embeddings import EmbeddingProvider, HashingEmbeddings

LOG = logging.getLogger("hippo.evals.external")

ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Derived from the benchmark's own ids, so two runs of the same file produce
# the same rows and a diff between runs is a real change.
NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _id(*parts: object) -> UUID:
    return uuid.uuid5(NAMESPACE, "|".join(str(part) for part in parts))


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    email: str | None = None
    groups: tuple[str, ...] = ()


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    title: str | None = None
    # Absent and empty mean different things. Absent is "this benchmark has no
    # permission model"; empty is "nobody may read this", which is a real and
    # testable state.
    readable_by: tuple[str, ...] | None = None
    occurred_at: str | None = None


class Question(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    asker: str
    answer_in: tuple[str, ...] = ()
    must_not_see: tuple[str, ...] = ()


class Benchmark(BaseModel):
    """Somebody else's benchmark, in the one shape this runner reads."""

    model_config = ConfigDict(frozen=True)

    name: str
    source: str | None = None
    people: tuple[Person, ...] = ()
    documents: tuple[Document, ...] = Field(default_factory=tuple)
    questions: tuple[Question, ...] = Field(default_factory=tuple)

    @property
    def has_permission_model(self) -> bool:
        """Whether this benchmark can say anything about the central claim.

        One document with a stated audience is enough to make the question
        meaningful; none at all means every answer is trivially permitted and
        a leak count of zero would be an artefact of the corpus, not a result.
        """
        return any(document.readable_by is not None for document in self.documents)

    @classmethod
    def load(cls, path: Path) -> Benchmark:
        return cls.model_validate_json(path.read_text())


class QuestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    retrieved: tuple[str, ...]
    expected: tuple[str, ...]
    forbidden_seen: tuple[str, ...]

    @property
    def recall(self) -> float:
        if not self.expected:
            return 0.0
        found = sum(1 for want in self.expected if want in self.retrieved)
        return found / len(self.expected)

    @property
    def first_rank(self) -> int | None:
        for rank, document in enumerate(self.retrieved, start=1):
            if document in self.expected:
                return rank
        return None


class Result(BaseModel):
    """What a run of somebody else's benchmark showed."""

    model_config = ConfigDict(frozen=True)

    name: str
    k: int
    measured_permissions: bool
    results: tuple[QuestionResult, ...]

    @property
    def recall_at_k(self) -> float:
        scored = [r for r in self.results if r.expected]
        return sum(r.recall for r in scored) / len(scored) if scored else 0.0

    @property
    def mrr(self) -> float:
        scored = [r for r in self.results if r.expected]
        if not scored:
            return 0.0
        return sum(0.0 if r.first_rank is None else 1.0 / r.first_rank for r in scored) / len(
            scored
        )

    @property
    def leaks(self) -> tuple[str, ...]:
        return tuple(
            f"{result.id}: {document}"
            for result in self.results
            for document in result.forbidden_seen
        )

    def summary(self) -> str:
        lines = [
            f"benchmark      {self.name}",
            f"questions      {len(self.results)}",
            f"recall@{self.k:<8} {self.recall_at_k:.3f}",
            f"mrr            {self.mrr:.3f}",
        ]
        if self.measured_permissions:
            lines.append(f"leaks          {len(self.leaks)}")
            lines.extend(f"  {leak}" for leak in self.leaks)
        else:
            # The distinction this whole module exists to preserve.
            lines.append(
                "leaks          not measured — this benchmark has no permission model, "
                "so it cannot test the claim that matters here"
            )
        return "\n".join(lines)


def load_into(conn: Connection, benchmark: Benchmark, embedder: EmbeddingProvider) -> None:
    """Write a benchmark into a Hippo database as ordinary content.

    Deliberately no special path: documents become entities and chunks, and
    audiences become acl_grants, so retrieval runs through exactly the filter
    that serves real queries. A benchmark loaded through a side door would
    measure a system nobody uses.
    """
    groups: dict[str, UUID] = {}
    for person in benchmark.people:
        for group in person.groups:
            groups.setdefault(group, _id("group", group))

    for group, group_id in groups.items():
        conn.execute(
            "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'group', %s) "
            "ON CONFLICT DO NOTHING",
            (group_id, f"bench-group-{group}"),
        )

    for person in benchmark.people:
        principal = _id("person", person.id)
        conn.execute(
            "INSERT INTO principals (id, kind, email, source_id) VALUES (%s, 'user', %s, %s) "
            "ON CONFLICT DO NOTHING",
            (principal, person.email, f"bench-user-{person.id}"),
        )
        for group in person.groups:
            conn.execute(
                "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (groups[group], principal),
            )

    everyone = [_id("person", person.id) for person in benchmark.people]

    for document in benchmark.documents:
        entity = _id("document", document.id)
        conn.execute(
            "INSERT INTO entities (id, entity_type, title, canonical_key, occurred_at) "
            "VALUES (%s, 'document', %s, %s, %s) ON CONFLICT DO NOTHING",
            (entity, document.title, f"bench:{document.id}", document.occurred_at),
        )

        if document.readable_by is None:
            audience = everyone
        else:
            audience = [
                groups[name] if name in groups else _id("person", name)
                for name in document.readable_by
            ]
        for principal in audience:
            conn.execute(
                "INSERT INTO acl_grants (entity_id, principal_id, source) "
                "VALUES (%s, %s, 'benchmark') ON CONFLICT DO NOTHING",
                (entity, principal),
            )

        (vector,) = embedder.embed([document.text])
        conn.execute(
            "INSERT INTO chunks (id, entity_id, scope_id, content, chunk_index, embedding) "
            "VALUES (%s, %s, %s, %s, 0, %s) ON CONFLICT DO NOTHING",
            (_id("chunk", document.id), entity, ORG_SCOPE, document.text, _pgvector(vector)),
        )


def _pgvector(vector: list[float]) -> str:
    from resolver.embeddings import to_pgvector

    return to_pgvector(vector)


def run(
    conn: Connection,
    benchmark: Benchmark,
    *,
    k: int = 20,
    embedder: EmbeddingProvider | None = None,
) -> Result:
    """Ask every question as the person it belongs to."""
    provider = embedder or HashingEmbeddings()
    by_chunk = {str(_id("chunk", document.id)): document.id for document in benchmark.documents}

    results = []
    for question in benchmark.questions:
        asker = _id("person", question.asker)
        hits = retrieve(conn, asker, plan_query(question.question, k=k), provider)
        retrieved = tuple(
            by_chunk[str(hit.chunk_id)] for hit in hits if str(hit.chunk_id) in by_chunk
        )
        results.append(
            QuestionResult(
                id=question.id,
                retrieved=retrieved,
                expected=question.answer_in,
                forbidden_seen=tuple(d for d in question.must_not_see if d in retrieved),
            )
        )

    return Result(
        name=benchmark.name,
        k=k,
        measured_permissions=benchmark.has_permission_model,
        results=tuple(results),
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    benchmark = Benchmark.load(Path(argv[0]))
    # scratch_database also *creates* it. The old default named a database
    # that had never been made, so the no-argument path could not work.
    target = argv[1] if len(argv) > 1 else scratch_database("hippo_bench")

    with connect(target, autocommit=True) as conn:
        upgrade(conn)

    with connect(target) as conn:
        load_into(conn, benchmark, HashingEmbeddings())
        conn.commit()
        result = run(conn, benchmark)

    print(result.summary())
    if benchmark.source:
        print(f"source         {benchmark.source}")
    return 1 if result.leaks else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
