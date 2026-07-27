"""A graph built to be measured on.

The fixture corpus is sixteen chunks. Running the golden set against it gave
recall@20 = 1.000 and vector recall = 1.000, and both numbers were empty: with
a k of 20 over sixteen chunks, retrieval returns everything, so "did the right
chunk come back" is true by arithmetic rather than by ranking. Graph expansion
scored zero for the same reason — every entity was already a hop-0 seed, so
there was nothing left for traversal to reach.

PROJECT.md asks for a *seeded-graph* golden-answer suite, and this is why. The
corpus below is generated so that each retrieval mode has something only it can
find:

**Lexical.** A fact stated with distinctive, unusual words. Keyword search
should find it; a semantic embedding might not, if the words are rare enough.

**Semantic.** The same fact restated in a paraphrase that shares no content
words with the question. Keyword search cannot find these, by construction —
which makes the FTS column of the report a real measurement rather than a
formality, and gives STACK.md's graduation trigger something to be evaluated
against.

**Traversal.** A fact whose chunk shares nothing with the question, sitting one
reply away from a chunk that matches it strongly. Only graph expansion reaches
it, so the graph column stops being zero for a reason rather than by accident.

**Forbidden.** Facts in private channels, for the red team to fail to reach.

Deterministic: a fixed seed, no clock, no randomness that is not derived from
it. An eval whose numbers move between runs is an eval nobody trusts enough to
gate on.
"""

from __future__ import annotations

import hashlib
import random
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict

from core.db import Connection
from resolver.embeddings import EmbeddingProvider, HashingEmbeddings

SEED = 20260725
ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Ids are derived rather than random. The filter breaks score ties with
# `ORDER BY score DESC, c.id`, and graph hits at the same hop tie constantly —
# so random ids move the numbers between runs by a few points. An eval that
# flaps is an eval whose failures get re-run rather than read.
NAMESPACE = UUID("f1e2d3c4-b5a6-4978-8899-aabbccddeeff")


def _id(*parts: object) -> UUID:
    return uuid5(NAMESPACE, ":".join(str(part) for part in parts))


# Deliberately odd words. A fact tagged with one of these is findable by
# keyword search and by nothing else, because no other chunk shares the token.
RARE = (
    "quillfeather",
    "brackenhold",
    "vantablack",
    "zephyrine",
    "morrowind",
    "cindermoth",
    "halcyonic",
    "obsidianite",
    "wraithbone",
    "gossamere",
    "pyrelight",
    "thornwick",
)

# Paraphrase pairs: the question uses the left phrasing, the corpus uses the
# right one, and they share no content word. Keyword search cannot bridge these.
PARAPHRASES = (
    ("what halted the shipment", "the delivery was brought to a standstill"),
    ("who signs off on spending", "expenditure requires the approval of the comptroller"),
    ("when does the contract lapse", "the agreement terminates at the end of the quarter"),
    ("how many seats did we sell", "we moved four hundred licences last period"),
    ("why did the customer leave", "the account churned after the outage"),
    ("what is the fault", "the defect lies in the caching layer"),
)

# Real messages that will never answer a question. Present because a corpus
# without them cannot measure curation: demoting noise only helps if there is
# noise to demote, and a generator that produced only useful content would make
# P2-MEM-2 look like it did nothing.
NOISE = (
    "+1",
    "thanks",
    "lgtm",
    "ok",
    ":shipit:",
    "haha",
    "on it",
    "same",
)

# One sentence repeated everywhere: a signature, a bot footer, a standing
# reminder. Individually harmless, collectively a wall between a question and
# its answer.
BOILERPLATE = "This channel is archived nightly. See the handbook for retention policy."

FILLER = (
    "standup notes for the week ahead",
    "reminder about the office move",
    "the build is green again",
    "lunch order going in at noon",
    "please review the draft when you get a chance",
    "moving this discussion to a thread",
    "quarterly planning starts monday",
    "the dashboard is back up",
    "welcome to the team",
    "shipping the patch this afternoon",
)


class Fact(BaseModel):
    """One planted answer, and the question it answers."""

    model_config = ConfigDict(frozen=True)

    kind: str
    question: str
    answer: str
    channel: str
    private: bool


class Corpus(BaseModel):
    """What was planted, so the golden set can be derived rather than guessed."""

    model_config = ConfigDict(frozen=True)

    facts: tuple[Fact, ...]
    chunks: int
    channels: int
    members: dict[str, tuple[str, ...]]


def build(
    conn: Connection,
    *,
    channels: int = 24,
    messages: int = 18,
    embedder: EmbeddingProvider | None = None,
) -> Corpus:
    """Generate and insert the graph. Returns what it planted.

    Sized so that the vector mode's internal limit (k * 4) is well under the
    corpus. That is the whole point: a mode has to choose, and a mode that
    returns everything is not being measured.
    """
    rng = random.Random(SEED)
    embed = embedder if embedder is not None else HashingEmbeddings()
    connector_id = _id("connector")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) "
            "VALUES (%s, 'slack', 'Eval workspace', '{}')",
            (connector_id,),
        )

    people = [f"U-EVAL-{index:02d}" for index in range(12)]
    principals: dict[str, UUID] = {}
    with conn.cursor() as cur:
        for source_id in people:
            principal = _id("principal", source_id)
            cur.execute(
                "INSERT INTO principals (id, kind, connector_id, source_id, email) "
                "VALUES (%s, 'user', %s, %s, %s)",
                (principal, connector_id, source_id, f"{source_id.lower()}@example.com"),
            )
            principals[source_id] = principal

    facts: list[Fact] = []
    members: dict[str, tuple[str, ...]] = {}
    total_chunks = 0

    for index in range(channels):
        name = f"C-EVAL-{index:02d}"
        private = index % 3 == 0
        # Every private channel has a different membership, so "who can see
        # this" varies rather than being one bit for the whole corpus.
        roster = tuple(rng.sample(people, k=rng.randint(2, 5))) if private else tuple(people)
        members[name] = roster

        channel_entity = _entity(conn, "channel", f"#{name}", name)
        _grant(conn, channel_entity, [principals[person] for person in roster])

        previous: UUID | None = None
        for position in range(messages):
            text, fact = _message(rng, index, position, name, private)
            key = f"{name}:{position}"
            entity = _entity(conn, "message", text[:60], key)
            _grant(conn, entity, [principals[person] for person in roster])
            _chunk(conn, entity, text, key, embed)
            total_chunks += 1

            _edge(conn, entity, channel_entity, "belongs_to")
            if previous is not None:
                _edge(conn, entity, previous, "replies_to")
            previous = entity

            if fact is not None:
                facts.append(fact)

            if position == 16:
                anchor = entity

        # The ticket the anchor message referenced. Granted to the same roster,
        # so the case measures retrieval rather than permission — and a private
        # channel's ticket stays private, which the denied case checks.
        ticket_key = _ticket_key(index)
        ticket = _entity(conn, "ticket", ticket_key, f"ticket:{ticket_key}")
        _grant(conn, ticket, [principals[person] for person in roster])
        answer = f"waiting on the compliance sign-off before anything ships, per {name}"
        _chunk(conn, ticket, answer, f"ticket:{ticket_key}", embed)
        total_chunks += 1
        _edge(conn, anchor, ticket, "references")
        facts.append(
            Fact(
                kind="crossref",
                question=f"what is the hold-up on {ticket_key}",
                answer=answer,
                channel=name,
                private=private,
            )
        )

    return Corpus(facts=tuple(facts), chunks=total_chunks, channels=channels, members=members)


def _message(
    rng: random.Random, channel_index: int, position: int, channel: str, private: bool
) -> tuple[str, Fact | None]:
    """One message, and the fact it plants if it plants one."""
    # Lexical: a rare token nothing else in the corpus contains.
    if position == 3:
        token = RARE[channel_index % len(RARE)] + f"{channel_index}"
        text = f"the {token} incident is still open and needs an owner"
        return text, Fact(
            kind="lexical",
            question=f"what is the status of {token}",
            answer=text,
            channel=channel,
            private=private,
        )

    # Semantic: the corpus states it one way, the question asks another, and
    # they share no content word.
    if position == 7:
        asked, stated = PARAPHRASES[channel_index % len(PARAPHRASES)]
        text = f"{stated}, per the review in {channel}"
        return text, Fact(
            kind="semantic",
            question=asked,
            answer=text,
            channel=channel,
            private=private,
        )

    # Traversal: an anchor with a rare token, and the answer in the message
    # that replies to it, sharing nothing with the question.
    if position == 11:
        token = "anchor" + RARE[(channel_index + 5) % len(RARE)] + f"{channel_index}"
        return f"opening the {token} thread for discussion", None
    if position == 12:
        token = "anchor" + RARE[(channel_index + 5) % len(RARE)] + f"{channel_index}"
        text = f"resolved by rolling forward the migration on node {channel_index}"
        return text, Fact(
            kind="traversal",
            question=f"tell me about {token}",
            answer=text,
            channel=channel,
            private=private,
        )

    # A cross-system reference (P3-GRF-2). The message names a ticket key and
    # says nothing else; the answer lives in the ticket, sharing no words with
    # the question. Only the `references` edge connects them, so this case is
    # zero unless the graph is working — which is the point of having it.
    if position == 16:
        return f"tracked in {_ticket_key(channel_index)}", None

    # Noise and boilerplate, at roughly the density a real workspace has. The
    # positions are fixed rather than random so the corpus stays reproducible.
    if position in (1, 5, 9, 14):
        return NOISE[(channel_index + position) % len(NOISE)], None
    if position in (2, 16):
        return BOILERPLATE, None

    return f"{rng.choice(FILLER)} ({channel} {position})", None


# ---------------------------------------------------------------------------
# Insert helpers. Written out rather than routed through the resolver: this is
# a graph built to a specification, not one discovered from raw records, and
# going through extraction would make the specification implicit.
# ---------------------------------------------------------------------------


def _ticket_key(channel_index: int) -> str:
    """A Jira-shaped key unique to one channel.

    Rare on purpose: the question is the key, so a key that appeared anywhere
    else would let lexical search answer a case meant to need a graph hop.
    """
    return f"EVL{channel_index:02d}-{100 + channel_index}"


def _entity(conn: Connection, entity_type: str, title: str, key: str) -> UUID:
    entity_id = _id("entity", entity_type, key)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, %s, %s)",
            (entity_id, entity_type, title),
        )
    return entity_id


def _grant(conn: Connection, entity_id: UUID, principal_ids: list[UUID]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'eval') "
            "ON CONFLICT DO NOTHING",
            [(entity_id, principal_id) for principal_id in principal_ids],
        )


def _chunk(
    conn: Connection, entity_id: UUID, content: str, key: str, embedder: EmbeddingProvider
) -> None:
    """One chunk, embedded by whichever model is being evaluated.

    Parameterised because comparing embedding models means embedding the same
    corpus with each of them and asking the same questions. A harness that
    hardcoded one would be able to measure retrieval but never the thing
    retrieval most depends on.
    """
    from resolver.embeddings import to_pgvector

    (vector,) = embedder.embed([content])
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chunks (id, entity_id, scope_id, content, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector)",
            (_id("chunk", key), entity_id, ORG_SCOPE, content, to_pgvector(vector)),
        )


def _edge(conn: Connection, src: UUID, dst: UUID, edge_type: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
            "VALUES (%s, %s, %s, 'source', 1.0) ON CONFLICT DO NOTHING",
            (src, dst, edge_type),
        )


def fingerprint(corpus: Corpus) -> str:
    """A hash of what was planted.

    The generator is deterministic, so this should not move. When it does, the
    numbers in the report are not comparable with yesterday's and nobody should
    read a change in them as a change in retrieval.
    """
    material = "|".join(f"{fact.kind}:{fact.question}:{fact.answer}" for fact in corpus.facts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
