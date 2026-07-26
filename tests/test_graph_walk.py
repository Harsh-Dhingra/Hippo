"""P3-GRF-1: the walk, and why direction is the whole idea.

The graph is what makes this more than a search box, and until this migration
it was contributing almost nothing and would have stopped working entirely on a
real corpus. Both problems turned out to be one problem.

Every explosion runs from the low-cardinality side of an edge to the
high-cardinality side — a person to their messages, a channel to its contents.
Every useful traversal runs the other way: a message to its author, a message
to its channel. So an edge is traversable per direction, the dangerous
direction is closed, and the fan-out is bounded by construction rather than by
a cap somebody has to tune.

Measured, controlled, on identical rows with the same HNSW graph:

    k     recall  ->  recall     traversal  ->  traversal
    10     0.540      0.747        0.000         0.833
    12     0.655      0.747        0.417         0.792
    20     0.747      0.747        0.750         0.750

Closing container-siblings *improved* traversal recall rather than costing it,
which is the finding worth keeping: arbitrary siblings from a busy channel were
crowding out the conversational path that actually held the answer.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agent.retrieval import RetrievalPlan, retrieve
from core.db import Connection
from resolver.embeddings import HashingEmbeddings

pytestmark = pytest.mark.requires_db

ORG = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def alice(migrated: Connection) -> UUID:
    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    return principal


def thing(
    conn: Connection,
    principal: UUID | None,
    entity_type: str,
    title: str,
    content: str | None = None,
) -> UUID:
    entity = uuid4()
    conn.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, %s, %s)",
        (entity, entity_type, title),
    )
    if principal is not None:
        conn.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
            (entity, principal),
        )
    if content is not None:
        conn.execute(
            "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) VALUES (%s, %s, %s, 0)",
            (entity, ORG, content),
        )
    return entity


def link(conn: Connection, src: UUID, dst: UUID, edge_type: str, confidence: float = 1.0) -> None:
    provenance = "source" if confidence >= 1.0 else "model"
    conn.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
        "VALUES (%s, %s, %s, %s, %s)",
        (src, dst, edge_type, provenance, confidence),
    )


def found(
    conn: Connection, principal: UUID, question: str, *, k: int = 50, hops: int = 2
) -> list[str]:
    return [
        hit.content
        for hit in retrieve(
            conn,
            principal,
            RetrievalPlan(query_text=question, k=k, hops=hops),
            HashingEmbeddings(),
        )
    ]


# ---------------------------------------------------------------------------
# The explosions, closed.
# ---------------------------------------------------------------------------


def test_a_persons_whole_history_is_not_one_hop_from_one_message(
    migrated: Connection, alice: UUID
) -> None:
    """The bug that made a two-hop walk unusable. A seed message reached its
    author, and the author reached every message they had ever written."""
    author = thing(migrated, alice, "person", "Busy Person")
    seed = thing(migrated, alice, "message", "seed", "the acme renewal liability cap")
    link(migrated, author, seed, "authored")

    for index in range(200):
        other = thing(migrated, alice, "message", f"m{index}", f"unrelated chatter {index}")
        link(migrated, author, other, "authored")

    contents = found(migrated, alice, "acme renewal liability cap")

    assert "the acme renewal liability cap" in contents
    assert not any("unrelated chatter" in text for text in contents)


def test_a_channels_contents_are_not_reachable_from_one_of_them(
    migrated: Connection, alice: UUID
) -> None:
    """The other explosion. Also, measurably, the right call: closing this
    raised traversal recall rather than lowering it, because arbitrary siblings
    from a busy channel crowded out the conversational path."""
    channel = thing(migrated, alice, "channel", "#busy")
    seed = thing(migrated, alice, "message", "seed", "the acme renewal liability cap")
    link(migrated, seed, channel, "belongs_to")

    for index in range(200):
        other = thing(migrated, alice, "message", f"m{index}", f"unrelated chatter {index}")
        link(migrated, other, channel, "belongs_to")

    contents = found(migrated, alice, "acme renewal liability cap")

    assert "the acme renewal liability cap" in contents
    assert not any("unrelated chatter" in text for text in contents)


def test_a_person_mentioned_does_not_drag_in_everything_that_mentioned_them(
    migrated: Connection, alice: UUID
) -> None:
    person = thing(migrated, alice, "person", "Mentioned")
    seed = thing(migrated, alice, "message", "seed", "the acme renewal liability cap")
    link(migrated, seed, person, "mentions")
    for index in range(50):
        other = thing(migrated, alice, "message", f"m{index}", f"unrelated chatter {index}")
        link(migrated, other, person, "mentions")

    contents = found(migrated, alice, "acme renewal liability cap")

    assert not any("unrelated chatter" in text for text in contents)


# ---------------------------------------------------------------------------
# The useful directions, kept.
# ---------------------------------------------------------------------------


def test_a_reply_to_the_match_is_reached(migrated: Connection, alice: UUID) -> None:
    """The path the eval's traversal cases actually take, and the one worth
    protecting: the answer is the message after the one that matched, sharing
    none of its words."""
    asked = thing(migrated, alice, "message", "q", "opening the anchorzebra thread")
    answer = thing(migrated, alice, "message", "a", "resolved by rolling forward node 4")
    link(migrated, answer, asked, "replies_to")

    contents = found(migrated, alice, "anchorzebra")

    assert "resolved by rolling forward node 4" in contents


def test_the_message_being_replied_to_is_reached(migrated: Connection, alice: UUID) -> None:
    """Both ways along a conversation. A thread is bounded, so both are safe."""
    parent = thing(migrated, alice, "message", "p", "the decision was eighteen percent")
    reply = thing(migrated, alice, "message", "r", "acknowledged anchorwalrus")
    link(migrated, reply, parent, "replies_to")

    contents = found(migrated, alice, "anchorwalrus")

    assert "the decision was eighteen percent" in contents


def test_two_things_declared_the_same_reach_each_other(migrated: Connection, alice: UUID) -> None:
    """P3-RES-1's same_as edges. The strongest relationship there is: a
    neighbour across one is as relevant as whatever reached it."""
    left = thing(migrated, alice, "account", "Acme Corporation", "anchorlynx account record")
    right = thing(migrated, alice, "account", "ACME Inc", "the renewal is blocked on legal")
    link(migrated, left, right, "same_as", confidence=0.95)

    contents = found(migrated, alice, "anchorlynx")

    assert "the renewal is blocked on legal" in contents


# ---------------------------------------------------------------------------
# Weighting.
# ---------------------------------------------------------------------------


def test_a_stronger_relationship_outranks_a_weaker_one(migrated: Connection, alice: UUID) -> None:
    """Weight decides how many rank places a hop costs. A conversational reply
    should beat the container the message happens to sit in."""
    seed = thing(migrated, alice, "message", "s", "anchorotter is the topic here")
    reply = thing(migrated, alice, "message", "r", "the reply that follows it")
    channel = thing(migrated, alice, "channel", "#c", "the channel description")
    link(migrated, reply, seed, "replies_to")
    link(migrated, seed, channel, "belongs_to")

    contents = found(migrated, alice, "anchorotter")

    assert contents.index("the reply that follows it") < contents.index("the channel description")


def test_an_inferred_edge_is_worth_less_than_a_stated_one(
    migrated: Connection, alice: UUID
) -> None:
    """Edge confidence multiplies into the weight, so a model's conclusion
    reaches less far than the same relationship a source stated. Rule 5, in the
    ranking rather than only in the schema."""
    seed = thing(migrated, alice, "account", "s", "anchorbadger is the subject")
    stated = thing(migrated, alice, "account", "certain", "the stated match")
    guessed = thing(migrated, alice, "account", "guessed", "the inferred match")
    link(migrated, seed, stated, "same_as", confidence=1.0)
    link(migrated, seed, guessed, "same_as", confidence=0.5)

    contents = found(migrated, alice, "anchorbadger")

    assert contents.index("the stated match") < contents.index("the inferred match")


def test_an_unknown_edge_type_is_traversable_forward_only(
    migrated: Connection, alice: UUID
) -> None:
    """A new relationship should be able to help without a migration, and
    should not be able to explode without one."""
    seed = thing(migrated, alice, "message", "s", "anchorheron is the subject")
    forward = thing(migrated, alice, "message", "f", "reached going forward")
    backward = thing(migrated, alice, "message", "b", "not reached going backward")
    link(migrated, seed, forward, "invented_relation")
    link(migrated, backward, seed, "invented_relation")

    contents = found(migrated, alice, "anchorheron")

    assert "reached going forward" in contents
    assert "not reached going backward" not in contents


def test_two_weak_hops_reach_further_down_than_one_strong_one(
    migrated: Connection, alice: UUID
) -> None:
    """Strength multiplies across hops, so a neighbour of a neighbour is only
    interesting when both steps were."""
    seed = thing(migrated, alice, "message", "s", "anchorstoat is the subject")
    near = thing(migrated, alice, "message", "n", "one strong hop away")
    far_a = thing(migrated, alice, "message", "fa", "first weak hop")
    far_b = thing(migrated, alice, "message", "fb", "two weak hops away")
    link(migrated, near, seed, "replies_to")
    link(migrated, seed, far_a, "invented_relation")
    link(migrated, far_a, far_b, "invented_relation")

    contents = found(migrated, alice, "anchorstoat")

    assert contents.index("one strong hop away") < contents.index("two weak hops away")


# ---------------------------------------------------------------------------
# Bounds.
# ---------------------------------------------------------------------------


def test_a_huge_thread_is_capped(migrated: Connection, alice: UUID) -> None:
    """The one direction that is legitimately unbounded. Direction weights
    remove the explosions; this is the backstop for a thread nobody stopped."""
    parent = thing(migrated, alice, "message", "p", "anchorsable opens the thread")
    for index in range(200):
        reply = thing(migrated, alice, "message", f"r{index}", f"reply number {index}")
        link(migrated, reply, parent, "replies_to")

    contents = found(migrated, alice, "anchorsable", k=500)

    replies = [text for text in contents if text.startswith("reply number")]
    assert 0 < len(replies) <= 25


def test_no_hops_means_no_graph_at_all(migrated: Connection, alice: UUID) -> None:
    seed = thing(migrated, alice, "message", "s", "anchormarten is the subject")
    reply = thing(migrated, alice, "message", "r", "the reply nobody asked for")
    link(migrated, reply, seed, "replies_to")

    contents = found(migrated, alice, "anchormarten", hops=0)

    assert "the reply nobody asked for" not in contents


def test_a_seed_is_not_reported_as_reached_by_graph(migrated: Connection, alice: UUID) -> None:
    """A chunk that matched directly is a direct hit. Labelling it as reached
    by the graph too would overstate what the graph found."""
    seed = thing(migrated, alice, "message", "s", "anchorermine is the subject")
    reply = thing(migrated, alice, "message", "r", "anchorermine again in the reply")
    link(migrated, reply, seed, "replies_to")

    hits = retrieve(
        migrated,
        alice,
        RetrievalPlan(query_text="anchorermine", k=20, hops=2),
        HashingEmbeddings(),
    )

    for hit in hits:
        if "is the subject" in hit.content:
            assert "graph" not in hit.retrieval_modes


# ---------------------------------------------------------------------------
# The thing that must never change.
# ---------------------------------------------------------------------------


def test_the_walk_still_cannot_widen_what_anybody_sees(migrated: Connection, alice: UUID) -> None:
    """The regression guard. Every hop joins visible_entities, so an edge can
    only reach something the asker already holds a grant for — whatever the
    edge says, whoever created it, however strong its weight."""
    mine = thing(migrated, alice, "message", "mine", "anchorquoll is mine to read")
    theirs = thing(migrated, None, "message", "theirs", "the secret nobody granted me")
    link(migrated, theirs, mine, "replies_to")
    link(migrated, mine, theirs, "same_as", confidence=0.99)

    contents = found(migrated, alice, "anchorquoll")

    assert "anchorquoll is mine to read" in contents
    assert "the secret nobody granted me" not in contents


def test_a_two_hop_route_through_a_visible_thing_still_cannot_reach_a_hidden_one(
    migrated: Connection, alice: UUID
) -> None:
    """The subtler version: a legitimate first hop, then an edge to something
    ungranted."""
    seed = thing(migrated, alice, "message", "s", "anchornumbat is the subject")
    middle = thing(migrated, alice, "message", "m", "a message I can read")
    hidden = thing(migrated, None, "message", "h", "a message I cannot read")
    link(migrated, middle, seed, "replies_to")
    link(migrated, hidden, middle, "replies_to")

    contents = found(migrated, alice, "anchornumbat")

    assert "a message I can read" in contents
    assert "a message I cannot read" not in contents


# ---------------------------------------------------------------------------
# The weights themselves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edge_type", "forward", "expected_zero"),
    [
        ("authored", True, True),
        ("authored", False, False),
        ("belongs_to", True, False),
        ("belongs_to", False, True),
        ("mentions", True, False),
        ("mentions", False, True),
        ("assigned_to", False, True),
        ("replies_to", True, False),
        ("replies_to", False, False),
        ("same_as", True, False),
        ("same_as", False, False),
    ],
)
def test_the_closed_directions_are_exactly_the_fan_out_ones(
    migrated: Connection, edge_type: str, forward: bool, expected_zero: bool
) -> None:
    """A weight of zero is not a ranking decision, it is the bound. Pinned so a
    later tweak cannot quietly reopen an explosion."""
    with migrated.cursor() as cur:
        cur.execute("SELECT _edge_weight(%s, %s)", (edge_type, forward))
        weight = float((cur.fetchone() or (0,))[0])

    assert (weight == 0.0) is expected_zero


def test_the_dependency_weights_exist_before_the_edges_do(migrated: Connection) -> None:
    """blocks and resolved_by are what "what is blocking X" should be answered
    by. Nothing creates them yet — P3-GRF-2 does — and they are weighted first
    so that when they arrive they are already worth walking."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT _edge_weight('blocks', true), _edge_weight('resolved_by', true), "
            "       _edge_weight('references', true)"
        )
        row = cur.fetchone()

    assert row is not None
    assert all(float(value) > 0.5 for value in row)
