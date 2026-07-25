"""Identity resolution: candidates to canonical entities.

The second resolver stage. Extraction says what each source record claims to
be; this decides which claims are about the same thing, and writes the graph.

Three rules, tried in this order (ARCHITECTURE section 6):

1. **Source id.** This record has been resolved before, so it keeps its entity.
   Nothing else can be more certain than the source agreeing with itself, which
   is why it goes first and why re-running is stable.
2. **Canonical key.** For people that is the lowercased email, set by
   extraction. It is the only thing a source states that identifies the same
   human in another system, so it is the rule the cross-system merge rests on.
3. **Normalized name.** For account-shaped entities, where two systems spell
   one company differently. Deliberately narrow: normalized-name matching on
   people would merge two different Alex Chens, and on messages it would merge
   anything said twice.

Model-assisted fuzzy matching is explicitly out of v0 (ARCHITECTURE section 6).
When it arrives it produces edges carrying provenance='model' and confidence
below 1.0, which is why every rule here is deterministic and every entity
records which rule claimed it.

A merge is recorded, not just performed. `attrs.resolved_by` says which rule
fired and entity_sources keeps every raw record behind the entity, so a wrong
merge can be found and explained rather than merely suspected.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from prometheus_client import Counter
from psycopg.types.json import Jsonb

from core.db import Connection
from resolver.extraction import EdgeCandidate, EntityCandidate, RawRecord, extract
from sync.connectors.sdk import SourceRef

LOG = logging.getLogger("hippo.resolver.resolution")

# Entity types where two systems spelling a name differently means one thing.
# Not people: two colleagues can share a name. Not content: two messages can
# share text.
NAME_MATCHED_TYPES = frozenset({"account"})

# Company suffixes that carry no identity. "Acme Inc" and "Acme Ltd" are the
# same customer written by two people, not two customers.
_LEGAL_SUFFIXES = frozenset(
    {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "sa", "bv", "ag"}
)
_PUNCTUATION = re.compile(r"[^\w\s]")

BY_SOURCE_ID = "source_id"
BY_CANONICAL_KEY = "canonical_key"
BY_NORMALIZED_NAME = "normalized_name"
NEW = "new"

MERGES = Counter(
    "hippo_resolver_merges_total", "Candidates merged into an existing entity.", ("rule",)
)
ENTITIES_CREATED = Counter("hippo_resolver_entities_created_total", "Entities created.")
EDGES_SKIPPED = Counter(
    "hippo_resolver_edges_skipped_total", "Edges dropped because an endpoint did not resolve."
)


def normalize_name(name: str) -> str:
    """Fold a company name to something two systems will agree on."""
    cleaned = _PUNCTUATION.sub(" ", name.lower())
    words = [word for word in cleaned.split() if word and word not in _LEGAL_SUFFIXES]
    return " ".join(words)


def merge_key(candidate: EntityCandidate) -> str | None:
    """The value rules 2 and 3 match on, or None if the candidate offers neither."""
    if candidate.canonical_key:
        return candidate.canonical_key
    if candidate.entity_type in NAME_MATCHED_TYPES and candidate.title:
        return normalize_name(candidate.title) or None
    return None


@dataclass
class ResolutionStats:
    """What one resolution pass did. Useful in logs and in tests."""

    created: int = 0
    merged: int = 0
    reattached: int = 0
    edges_written: int = 0
    edges_skipped: int = 0
    principals_linked: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    def record(self, rule: str) -> None:
        self.by_rule[rule] = self.by_rule.get(rule, 0) + 1
        if rule == NEW:
            self.created += 1
            ENTITIES_CREATED.inc()
        elif rule == BY_SOURCE_ID:
            self.reattached += 1
        else:
            self.merged += 1
            MERGES.labels(rule=rule).inc()


# ---------------------------------------------------------------------------
# Lookups.
# ---------------------------------------------------------------------------


def _entity_for_raw_record(conn: Connection, raw_record_id: UUID) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM entity_sources WHERE raw_record_id = %s", (raw_record_id,)
        )
        row = cur.fetchone()
    return None if row is None else UUID(str(row[0]))


def _entity_for_key(conn: Connection, entity_type: str, key: str) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM entities WHERE entity_type = %s AND canonical_key = %s",
            (entity_type, key),
        )
        row = cur.fetchone()
    return None if row is None else UUID(str(row[0]))


def entity_index(conn: Connection) -> dict[tuple[str, str], UUID]:
    """Source reference to entity id, for everything resolved so far.

    Edges are extracted as references between source objects, so this is what
    turns them into rows. It spans connectors on purpose: an edge from a Slack
    message to a person merged out of Slack and Jira has to land on the merged
    entity.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.source_type, r.source_id, es.entity_id "
            "FROM raw_records r JOIN entity_sources es ON es.raw_record_id = r.id"
        )
        return {(str(row[0]), str(row[1])): UUID(str(row[2])) for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------


def _create_entity(
    conn: Connection, candidate: EntityCandidate, key: str | None, rule: str
) -> UUID:
    attrs = dict(candidate.attrs)
    attrs["resolved_by"] = rule
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_type, canonical_key, title, attrs) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (candidate.entity_type, key, candidate.title, Jsonb(attrs)),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _absorb(conn: Connection, entity_id: UUID, candidate: EntityCandidate, rule: str) -> None:
    """Fold a candidate into an entity that already exists.

    The title is not overwritten. Whichever candidate created the entity named
    it, and candidates arrive in a sorted order, so a re-run reaches the same
    answer instead of the graph flickering between two spellings of one name.
    Attributes merge, because a second system usually knows something the first
    did not.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE entities SET "
            "    attrs = attrs || %s, "
            "    title = coalesce(title, %s) "
            "WHERE id = %s",
            (Jsonb({**candidate.attrs, "resolved_by": rule}), candidate.title, entity_id),
        )


def _link_source(conn: Connection, entity_id: UUID, raw_record_id: UUID) -> None:
    """The provenance trail. Every raw record behind an entity stays listed, so
    a merge can be explained after the fact."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (entity_id, raw_record_id),
        )


def resolve_candidate(
    conn: Connection, candidate: EntityCandidate, raw_record_id: UUID, stats: ResolutionStats
) -> UUID:
    """Apply the three rules in order and return the entity this candidate is."""
    existing = _entity_for_raw_record(conn, raw_record_id)
    if existing is not None:
        _absorb(conn, existing, candidate, BY_SOURCE_ID)
        stats.record(BY_SOURCE_ID)
        return existing

    key = merge_key(candidate)
    if key is not None:
        rule = BY_CANONICAL_KEY if candidate.canonical_key else BY_NORMALIZED_NAME
        match = _entity_for_key(conn, candidate.entity_type, key)
        if match is not None:
            _absorb(conn, match, candidate, rule)
            _link_source(conn, match, raw_record_id)
            stats.record(rule)
            LOG.info(
                "merged a candidate into an existing entity",
                extra={
                    "rule": rule,
                    "entity_id": str(match),
                    "entity_type": candidate.entity_type,
                    "key": key,
                    "source": f"{candidate.source.source_type}:{candidate.source.source_id}",
                },
            )
            return match

    entity_id = _create_entity(conn, candidate, key, NEW)
    _link_source(conn, entity_id, raw_record_id)
    stats.record(NEW)
    return entity_id


def _write_edge(conn: Connection, edge: EdgeCandidate, index: dict[tuple[str, str], UUID]) -> bool:
    src = index.get((edge.src.source_type, edge.src.source_id))
    dst = index.get((edge.dst.source_type, edge.dst.source_id))

    if src is None or dst is None:
        # A reference to something not synced: a mention of a departed user, a
        # thread parent outside the fetched window. Dropping is right; the
        # alternative is an entity invented from a reference, which would have
        # no ACL and would be a node nothing can see.
        missing = edge.src if src is None else edge.dst
        LOG.info(
            "dropped an edge with an unresolved endpoint",
            extra={
                "edge_type": edge.edge_type,
                "missing": f"{missing.source_type}:{missing.source_id}",
            },
        )
        EDGES_SKIPPED.inc()
        return False

    if src == dst:
        # Two endpoints that merged into one entity. A self-loop says nothing
        # and would confuse graph expansion.
        LOG.info("dropped a self-edge created by a merge", extra={"edge_type": edge.edge_type})
        EDGES_SKIPPED.inc()
        return False

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (src_id, dst_id, edge_type, provenance) DO NOTHING",
            (src, dst, edge.edge_type, edge.provenance, edge.confidence),
        )
    return True


# ---------------------------------------------------------------------------
# The pass.
# ---------------------------------------------------------------------------


def resolve_records(conn: Connection, records: Sequence[RawRecord]) -> ResolutionStats:
    """Resolve a batch of raw records into the graph.

    Two passes, because an edge cannot be written until both of its endpoints
    exist, and an endpoint is frequently in a different record than the edge.

    Idempotent by construction: entities are found by source id before anything
    else is tried, and edges are unique on (src, dst, type, provenance). Running
    this twice over the same records changes nothing, which is what makes
    re-resolution a normal operation rather than a repair.
    """
    stats = ResolutionStats()
    extracted = [(record, extract(record)) for record in records]

    # Sorted so that when two candidates merge, the same one creates the entity
    # and names it every time.
    entities: list[tuple[EntityCandidate, UUID]] = sorted(
        ((candidate, record.id) for record, result in extracted for candidate in result.entities),
        key=lambda pair: (pair[0].source.source_type, pair[0].source.source_id),
    )
    for candidate, raw_record_id in entities:
        resolve_candidate(conn, candidate, raw_record_id, stats)

    index = entity_index(conn)
    for _record, result in extracted:
        for edge in result.edges:
            if _write_edge(conn, edge, index):
                stats.edges_written += 1
            else:
                stats.edges_skipped += 1

    stats.principals_linked = link_principal_identities(conn)

    LOG.info(
        "resolution pass complete",
        extra={
            "created": stats.created,
            "merged": stats.merged,
            "reattached": stats.reattached,
            "edges_written": stats.edges_written,
            "edges_skipped": stats.edges_skipped,
            "principals_linked": stats.principals_linked,
            "by_rule": stats.by_rule,
        },
    )
    return stats


def link_principal_identities(conn: Connection) -> int:
    """Mark the accounts that belong to one human.

    Rule 2 above, applied to principals instead of entities. Merging Alice's
    Slack and Jira *entities* makes the graph say one person authored both; it
    does nothing for permissions, because a grant names an account in a source
    system and Alice holds two. Without this, no single asker could ever get an
    answer spanning two connectors — ARCHITECTURE section 12 point 1 is
    unreachable.

    What it does not do matters as much. It never merges accounts with no
    email, because an absent email is not a match. It never touches groups: a
    shared mailing address is not shared membership, and merging two groups
    would hand every member of one the grants of the other. And it only links
    accounts that have a counterpart, so identity_id always means "one of
    several" rather than "processed".

    Idempotent, and safe to run on every pass: an existing identity_id is
    reused rather than replaced, so linking never reshuffles ids that grants
    or audit records may already refer to.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH people AS ("
            "    SELECT array_agg(id) AS ids,"
            "           coalesce(min(identity_id::text)::uuid, gen_random_uuid()) AS identity"
            "    FROM principals"
            "    WHERE kind = 'user' AND email IS NOT NULL AND btrim(email) <> ''"
            "    GROUP BY lower(btrim(email))"
            "    HAVING count(*) > 1"
            ") "
            "UPDATE principals p SET identity_id = people.identity "
            "FROM people "
            "WHERE p.id = ANY (people.ids) "
            "  AND p.identity_id IS DISTINCT FROM people.identity"
        )
        linked = cur.rowcount

    if linked:
        LOG.info("linked accounts to people", extra={"principals": linked})
    return linked


def resolve_connector(conn: Connection, connector_id: UUID | None = None) -> ResolutionStats:
    """Resolve everything one connector has synced, or everything.

    Loads the batch into memory, which is fine at v0 scale and is where a
    caller would start batching if ARCHITECTURE section 8's five million record
    ceiling were ever approached.
    """
    from resolver.extraction import load_raw_records

    return resolve_records(conn, load_raw_records(conn, connector_id))


def sources_of(conn: Connection, entity_id: UUID) -> list[SourceRef]:
    """Every source object behind an entity. The merge, made inspectable."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.source_type, r.source_id FROM entity_sources es "
            "JOIN raw_records r ON r.id = es.raw_record_id "
            "WHERE es.entity_id = %s ORDER BY r.source_type, r.source_id",
            (entity_id,),
        )
        return [SourceRef(source_type=str(row[0]), source_id=str(row[1])) for row in cur.fetchall()]
