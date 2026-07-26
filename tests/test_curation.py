"""P2-MEM-2: deciding which memories deserve a retrieval slot.

PROJECT.md calls this the gap in everyone else. The reason it stays a gap is
that the obvious implementation is deletion, and deletion is wrong — so the
first group of tests here is about what curation refuses to do.

The second group is about where it actually helps, which turned out to be a
narrower place than expected and is worth being precise about. A controlled
experiment (same row rewrites, weights applied or reset) showed the signal
weights change search recall by exactly zero: retrieval already requires
lexical or semantic overlap, and "+1" has neither with any real question.

Where it does help is browse — "show me what you have" — which returns
everything visible and had a quarter of its top twenty spent on
acknowledgements and boilerplate footers. That is measured below rather than
asserted.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from resolver.curation import (
    SIGNAL_DUPLICATE,
    SIGNAL_LOW,
    SIGNAL_NOISE,
    curate,
    mark_superseded,
    recurate,
    score_signal,
)

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORG = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def world(migrated: Connection) -> UUID:
    entity = uuid4()
    migrated.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'm')", (entity,)
    )
    return entity


def add_chunk(conn: Connection, entity: UUID, content: str, index: int = 0) -> UUID:
    chunk_id = uuid4()
    conn.execute(
        "INSERT INTO chunks (id, entity_id, scope_id, content, chunk_index) "
        "VALUES (%s, %s, %s, %s, %s)",
        (chunk_id, entity, ORG, content, index),
    )
    return chunk_id


def signal_of(conn: Connection, chunk_id: UUID) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT signal FROM chunks WHERE id = %s", (chunk_id,))
        return float((cur.fetchone() or (0,))[0])


# ---------------------------------------------------------------------------
# The scoring rules. Explainable on sight, or nobody trusts them with a corpus.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["+1", "thanks", "Thanks!", "LGTM", "ok", "  yes  ", "👍", "...", "", "haha"],
)
def test_acknowledgements_are_noise(content: str) -> None:
    assert score_signal(content) == SIGNAL_NOISE


@pytest.mark.parametrize(
    "content",
    [
        "thanks for the detailed writeup on the caching layer",
        "no, the renewal is blocked on legal review",
        "done — the migration rolled forward cleanly on node 4",
    ],
)
def test_a_real_sentence_beginning_with_an_acknowledgement_is_kept(content: str) -> None:
    """Matched whole. "thanks" is noise; "thanks for the writeup on X" is the
    answer to a question about X."""
    assert score_signal(content) == 1.0


def test_short_text_with_an_identifier_is_kept() -> None:
    """The exact-identifier case is the whole reason hybrid retrieval exists;
    demoting "see ACME-1" would undo it."""
    assert score_signal("see ACME-1") == 1.0
    assert score_signal("ACME-1") == 1.0


def test_a_short_link_is_kept() -> None:
    assert score_signal("https://acme.atlassian.net/browse/ACME-1") == 1.0


def test_short_prose_is_demoted_but_not_silenced() -> None:
    """Between "never useful" and "useful": worth less than a paragraph, worth
    more than nothing."""
    assert score_signal("moving this to a thread") == SIGNAL_LOW
    assert SIGNAL_NOISE < SIGNAL_LOW < 1.0


def test_boilerplate_repeated_everywhere_is_demoted() -> None:
    """One copy of a footer is harmless. Fifty crowding out a real answer is
    not."""
    footer = "This channel is archived nightly. See the handbook for retention policy."

    assert score_signal(footer, copies=1) == 1.0
    assert score_signal(footer, copies=40) == SIGNAL_DUPLICATE


def test_ordinary_content_is_untouched() -> None:
    assert (
        score_signal("Legal have flagged the liability cap and nothing ships until agreed") == 1.0
    )


# ---------------------------------------------------------------------------
# What curation refuses to do.
# ---------------------------------------------------------------------------


def test_nothing_is_ever_deleted(migrated: Connection, world: UUID) -> None:
    """A system that quietly stops answering questions it could answer leaves
    the person asking unable to tell "we do not have that" from "we decided it
    was stale"."""
    noise = add_chunk(migrated, world, "+1")

    curate(migrated)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE id = %s", (noise,))
        assert cur.fetchone() == (1,)


def test_a_demoted_chunk_is_still_retrievable(migrated: Connection, world: UUID) -> None:
    """The line between ranking and visibility, tested directly: a chunk scored
    at the floor still comes back when nothing else does."""
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings

    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (world, principal),
    )
    add_chunk(migrated, world, "+1")
    curate(migrated)

    hits = retrieve(
        migrated, principal, RetrievalPlan(query_text="", k=10, hops=0), HashingEmbeddings()
    )

    assert [hit.content for hit in hits] == ["+1"]


def test_a_blank_question_browses(migrated: Connection, world: UUID) -> None:
    """Migration 003 documents browse as "what can I see", reached when neither
    query input is given. The filter tests for NULL, so retrieve() has to send
    NULL rather than an empty string — otherwise a blank box asks for
    everything matching nothing and gets nothing back.
    """
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings

    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (world, principal),
    )
    add_chunk(migrated, world, "the liability cap is the open question on this renewal")

    for blank in ("", "   "):
        hits = retrieve(
            migrated,
            principal,
            RetrievalPlan(query_text=blank, k=10, hops=0),
            HashingEmbeddings(),
        )
        assert len(hits) == 1, f"a blank query browses, not {blank!r} matching nothing"
        assert hits[0].retrieval_modes == ("browse",)


def test_the_signal_is_a_weight_the_database_enforces(migrated: Connection, world: UUID) -> None:
    from psycopg import errors

    chunk = add_chunk(migrated, world, "hello there everyone")

    with pytest.raises(errors.CheckViolation):
        migrated.execute("UPDATE chunks SET signal = 2.0 WHERE id = %s", (chunk,))
    migrated.rollback()


# ---------------------------------------------------------------------------
# The pass itself.
# ---------------------------------------------------------------------------


def test_a_pass_assesses_and_records(migrated: Connection, world: UUID) -> None:
    noise = add_chunk(migrated, world, "+1", 0)
    real = add_chunk(migrated, world, "the renewal is blocked on legal review of the cap", 1)

    stats = curate(migrated)

    assert stats.examined == 2
    assert stats.noise == 1
    assert signal_of(migrated, noise) == pytest.approx(SIGNAL_NOISE)
    assert signal_of(migrated, real) == pytest.approx(1.0)


def test_a_second_pass_does_nothing(migrated: Connection, world: UUID) -> None:
    """Idempotent, so it can run on a schedule over a large corpus."""
    add_chunk(migrated, world, "+1")
    curate(migrated)

    assert curate(migrated).examined == 0


def test_recurating_reassesses_everything(migrated: Connection, world: UUID) -> None:
    """What a rule change needs, and a separate verb because re-scoring a
    corpus is a decision."""
    add_chunk(migrated, world, "+1")
    curate(migrated)

    recurate(migrated)

    assert curate(migrated).examined == 1


def test_a_pass_can_be_scoped_to_one_connector(migrated: Connection) -> None:
    connector, other = uuid4(), uuid4()
    for connector_id, name in ((connector, "A"), (other, "B")):
        migrated.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', %s)",
            (connector_id, name),
        )
    for connector_id, text in ((connector, "+1"), (other, "thanks")):
        entity = uuid4()
        migrated.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'message')", (entity,))
        migrated.execute(
            "INSERT INTO raw_records (connector_id, source_type, source_id, payload) "
            "VALUES (%s, 'slack.message', %s, '{}') RETURNING id",
            (connector_id, str(entity)),
        )
        with migrated.cursor() as cur:
            cur.execute(
                "SELECT id FROM raw_records WHERE connector_id = %s LIMIT 1", (connector_id,)
            )
            raw = (cur.fetchone() or (None,))[0]
        migrated.execute(
            "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)",
            (entity, raw),
        )
        add_chunk(migrated, entity, text)

    assert curate(migrated, connector_id=connector).examined == 1


# ---------------------------------------------------------------------------
# Supersede: evidence, not a guess.
# ---------------------------------------------------------------------------


def said_at(conn: Connection, when: str) -> UUID:
    """An entity that occurred at a stated time, so "newer" means something."""
    entity = uuid4()
    conn.execute(
        "INSERT INTO entities (id, entity_type, title, occurred_at) "
        "VALUES (%s, 'message', 'm', %s)",
        (entity, when),
    )
    return entity


def test_an_identical_restatement_is_superseded(migrated: Connection) -> None:
    """The case where "this replaced that" is a fact rather than an inference:
    the same sentence stated again later in the same channel.

    Across entities, not within one: chunks are UNIQUE (entity_id,
    content_hash), so one entity can never hold the same text twice.
    """
    older = add_chunk(migrated, said_at(migrated, "2026-01-01"), "our floor is 18 percent")
    newer = add_chunk(migrated, said_at(migrated, "2026-06-01"), "our floor is 18 percent")

    assert mark_superseded(migrated) == 1

    with migrated.cursor() as cur:
        cur.execute("SELECT id, superseded_by FROM chunks ORDER BY id")
        pointers: dict[object, object] = dict(cur.fetchall())
    assert pointers[older] == newer, "the older points at the newer"
    assert pointers[newer] is None, "and the newest points at nothing"


def test_different_text_is_not_superseded(migrated: Connection) -> None:
    """Anything looser needs a model. Guessing here would demote a correct
    answer in favour of a later vaguer one, and that failure is silent."""
    add_chunk(migrated, said_at(migrated, "2026-01-01"), "our discount floor is 18 percent")
    add_chunk(migrated, said_at(migrated, "2026-06-01"), "the renewal is blocked on legal")

    assert mark_superseded(migrated) == 0


def test_the_same_text_in_a_different_scope_is_not_superseded(migrated: Connection) -> None:
    """Two channels saying the same thing are two facts, not a restatement.
    Boilerplate repeated across a corpus is the duplicate rule's job."""
    other_scope = uuid4()
    migrated.execute(
        "INSERT INTO memory_scopes (id, scope_type, name) VALUES (%s, 'org', 'other')",
        (other_scope,),
    )
    text = "deploys are frozen until the audit closes"
    add_chunk(migrated, said_at(migrated, "2026-01-01"), text)
    chunk_id = uuid4()
    migrated.execute(
        "INSERT INTO chunks (id, entity_id, scope_id, content, chunk_index) "
        "VALUES (%s, %s, %s, %s, 0)",
        (chunk_id, said_at(migrated, "2026-06-01"), other_scope, text),
    )

    assert mark_superseded(migrated) == 0


def test_a_chunk_cannot_supersede_itself(migrated: Connection, world: UUID) -> None:
    from psycopg import errors

    chunk = add_chunk(migrated, world, "some content here to be long enough")

    with pytest.raises(errors.CheckViolation):
        migrated.execute("UPDATE chunks SET superseded_by = id WHERE id = %s", (chunk,))
    migrated.rollback()


def test_a_superseded_chunk_is_demoted_not_hidden(migrated: Connection, world: UUID) -> None:
    """A superseded fact is still true history — "our floor was 18 percent" was
    correct in June — and the timeline is built on exactly that."""
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings

    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (world, principal),
    )
    older = add_chunk(migrated, world, "our discount floor is eighteen percent", 0)
    newer = add_chunk(migrated, world, "our discount floor is twenty two percent", 1)
    migrated.execute("UPDATE chunks SET superseded_by = %s WHERE id = %s", (newer, older))

    hits = retrieve(
        migrated,
        principal,
        RetrievalPlan(query_text="what is our discount floor", k=10, hops=0),
        HashingEmbeddings(),
    )

    contents = [hit.content for hit in hits]
    assert len(contents) == 2, "both are returned"
    assert contents[0].endswith("twenty two percent"), "and the current one wins"


# ---------------------------------------------------------------------------
# Where it actually helps, measured rather than asserted.
# ---------------------------------------------------------------------------


def test_curation_clears_noise_out_of_browse(migrated: Connection) -> None:
    """Browse returns everything visible, so this is where noise genuinely
    competes — unlike search, where a query has to match and "+1" matches
    nothing.

    Measured on a 108-chunk seeded corpus, noise in the top k:

        k=10     1 -> 0
        k=20     5 -> 0
        k=50    16 -> 2

    Two survive at k=50 because nothing is ever removed: once the real content
    runs out, demoted chunks are what is left. That is the intended behaviour,
    and the reason this asserts on the top twenty rather than on all of it.
    """
    from agent.retrieval import RetrievalPlan, retrieve
    from evals.seed import BOILERPLATE, NOISE, build
    from resolver.embeddings import HashingEmbeddings

    corpus = build(migrated, channels=6, messages=18)
    with migrated.cursor() as cur:
        cur.execute("SELECT id FROM principals WHERE source_id = 'U-EVAL-01'")
        principal = UUID(str((cur.fetchone() or (None,))[0]))

    def noisy_in_top(k: int) -> int:
        hits = retrieve(
            migrated, principal, RetrievalPlan(query_text="", k=k, hops=0), HashingEmbeddings()
        )
        assert len(hits) == k, "browse fills k, so this is a share of the same window"
        return sum(1 for hit in hits if hit.content.strip() in NOISE or hit.content == BOILERPLATE)

    before = noisy_in_top(20)
    curate(migrated)
    after = noisy_in_top(20)

    assert corpus.chunks > 100
    assert before >= 4, "the corpus has noise to clear"
    assert after == 0


def test_search_recall_is_unaffected(migrated: Connection) -> None:
    """Measured, and reported honestly: demoting noise does not improve search
    recall on this corpus, because retrieval already requires overlap and an
    acknowledgement has none with any real question.

    A controlled run — same row rewrites, weights applied or reset — put the
    delta at exactly zero. This pins that, so a future change that claims a
    recall win has to actually show one.
    """
    from evals.harness import cases_from, run
    from evals.seed import build

    corpus = build(migrated, channels=6, messages=18)
    cases = cases_from(corpus)
    before = run(migrated, cases, k=20)

    curate(migrated)
    after = run(migrated, cases, k=20)

    assert after.recall_at_k >= before.recall_at_k
    assert after.leaks == ()
