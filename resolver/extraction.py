"""Extraction: raw records to candidate entities and edges.

The first resolver stage, and the only one that is a pure function. Given a raw
record it returns what that record asserts about the world, with no database,
no model call, and no dependence on anything already resolved. Two consequences
follow from that and both are worth protecting.

**It is testable exactly.** A fixture corpus produces one specific set of
entities and edges, so the tests assert equality rather than properties. Every
mapping decision is pinned by something that fails when it changes.

**It is re-runnable.** Extraction reads raw records and writes nothing, so
fixing a mapping means changing code and running it again. Raw records are
immutable source truth (CLAUDE.md rule 4), which is what makes re-resolution a
normal operation rather than a recovery.

Candidates reference each other by source reference, never by entity id,
because at this stage no entity exists yet. Turning those references into rows
is P1-RES-2's job, and keeping the two stages apart is what lets identity
resolution change its rules without extraction changing at all.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.db import Connection
from sync.connectors.sdk import SourceRef

LOG = logging.getLogger("hippo.resolver.extraction")

# Edge vocabulary. Kept small on purpose: a graph with forty edge types is a
# graph nobody can query.
AUTHORED = "authored"
BELONGS_TO = "belongs_to"
MENTIONS = "mentions"
REPLIES_TO = "replies_to"
ASSIGNED_TO = "assigned_to"

# <@U0123ABC> in Slack message text.
SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9-]+)>")


class RawRecord(BaseModel):
    """A row of raw_records, as the resolver reads it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    connector_id: UUID
    source_type: str
    source_id: str
    payload: dict[str, Any]
    container: SourceRef | None = None

    @property
    def ref(self) -> SourceRef:
        return SourceRef(source_type=self.source_type, source_id=self.source_id)

    @property
    def connector_kind(self) -> str:
        """'slack' from 'slack.message'. Lets one extractor build references to
        the connector's other source types without being told which connector
        it belongs to."""
        return self.source_type.split(".", 1)[0]


class EntityCandidate(BaseModel):
    """What one source object claims to be."""

    model_config = ConfigDict(frozen=True)

    source: SourceRef
    entity_type: str
    title: str | None = None
    canonical_key: str | None = Field(
        default=None,
        description="What identity resolution may merge on. Only set where the "
        "source states something genuinely identifying, such as an email.",
    )
    attrs: dict[str, Any] = Field(default_factory=dict)


class EdgeCandidate(BaseModel):
    """A relation between two source objects."""

    model_config = ConfigDict(frozen=True)

    src: SourceRef
    dst: SourceRef
    edge_type: str
    provenance: str = "source"
    confidence: float = 1.0


class Extraction(BaseModel):
    """Everything a set of raw records asserts."""

    model_config = ConfigDict(frozen=True)

    entities: tuple[EntityCandidate, ...] = ()
    edges: tuple[EdgeCandidate, ...] = ()

    def __add__(self, other: Extraction) -> Extraction:
        return Extraction(entities=self.entities + other.entities, edges=self.edges + other.edges)


Extractor = Callable[[RawRecord], Extraction]


def _clip(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    flattened = " ".join(text.split())
    return flattened[:limit] or None


def _person_ref(record: RawRecord, account_id: str) -> SourceRef:
    """A reference to whoever did something, in the connector's own terms.

    The identities stream stores users as `<connector>.user`, so a message's
    author resolves to the same record the principal came from.
    """
    return SourceRef(source_type=f"{record.connector_kind}.user", source_id=account_id)


# ---------------------------------------------------------------------------
# Slack.
# ---------------------------------------------------------------------------


def extract_slack_channel(record: RawRecord) -> Extraction:
    name = record.payload.get("name")
    return Extraction(
        entities=(
            EntityCandidate(
                source=record.ref,
                entity_type="channel",
                title=f"#{name}" if name else record.source_id,
                attrs={
                    "is_private": bool(record.payload.get("is_private")),
                    "topic": (record.payload.get("topic") or {}).get("value"),
                },
            ),
        )
    )


def extract_slack_message(record: RawRecord) -> Extraction:
    payload = record.payload
    text = payload.get("text") or ""
    entities = (
        EntityCandidate(
            source=record.ref,
            entity_type="message",
            title=_clip(text),
            attrs={"ts": payload.get("ts"), "thread_ts": payload.get("thread_ts")},
        ),
    )

    edges: list[EdgeCandidate] = []
    if record.container is not None:
        edges.append(EdgeCandidate(src=record.ref, dst=record.container, edge_type=BELONGS_TO))

    author = payload.get("user")
    if author:
        edges.append(
            EdgeCandidate(src=_person_ref(record, str(author)), dst=record.ref, edge_type=AUTHORED)
        )

    # A reply points at the message that started the thread. The parent is
    # identified the same way this record is, so the reference resolves without
    # anything having been resolved yet.
    thread_ts = payload.get("thread_ts")
    if thread_ts and record.container is not None and str(thread_ts) != str(payload.get("ts")):
        edges.append(
            EdgeCandidate(
                src=record.ref,
                dst=SourceRef(
                    source_type=record.source_type,
                    source_id=f"{record.container.source_id}:{thread_ts}",
                ),
                edge_type=REPLIES_TO,
            )
        )

    for mentioned in sorted(set(SLACK_MENTION.findall(text))):
        edges.append(
            EdgeCandidate(src=record.ref, dst=_person_ref(record, mentioned), edge_type=MENTIONS)
        )

    return Extraction(entities=entities, edges=tuple(edges))


def extract_person(record: RawRecord) -> Extraction:
    """Users, from either connector.

    canonical_key is the lowercased email and nothing else. It is the one thing
    a source states that identifies the same human in another system, and
    P1-RES-2's merge rule is built on it. A person with no email gets no key
    and stays separate, which is the safe outcome.
    """
    payload = record.payload
    email = payload.get("emailAddress") or (payload.get("profile") or {}).get("email")
    name = payload.get("displayName") or (payload.get("profile") or {}).get("real_name")
    return Extraction(
        entities=(
            EntityCandidate(
                source=record.ref,
                entity_type="person",
                title=_clip(name) or record.source_id,
                canonical_key=str(email).lower() if email else None,
                attrs={"email": email},
            ),
        )
    )


# ---------------------------------------------------------------------------
# Jira.
# ---------------------------------------------------------------------------


def extract_jira_project(record: RawRecord) -> Extraction:
    return Extraction(
        entities=(
            EntityCandidate(
                source=record.ref,
                entity_type="project",
                title=_clip(record.payload.get("name")) or record.source_id,
                attrs={"key": record.payload.get("key")},
            ),
        )
    )


def extract_jira_issue(record: RawRecord) -> Extraction:
    fields = record.payload.get("fields") or {}
    entities = (
        EntityCandidate(
            source=record.ref,
            entity_type="ticket",
            title=_clip(fields.get("summary")) or record.source_id,
            attrs={
                "key": record.source_id,
                "status": (fields.get("status") or {}).get("name"),
            },
        ),
    )

    edges: list[EdgeCandidate] = []
    if record.container is not None:
        edges.append(EdgeCandidate(src=record.ref, dst=record.container, edge_type=BELONGS_TO))

    reporter = (fields.get("reporter") or {}).get("accountId")
    if reporter:
        edges.append(
            EdgeCandidate(
                src=_person_ref(record, str(reporter)), dst=record.ref, edge_type=AUTHORED
            )
        )

    assignee = (fields.get("assignee") or {}).get("accountId")
    if assignee:
        edges.append(
            EdgeCandidate(
                src=record.ref, dst=_person_ref(record, str(assignee)), edge_type=ASSIGNED_TO
            )
        )

    return Extraction(entities=entities, edges=tuple(edges))


def extract_jira_comment(record: RawRecord) -> Extraction:
    payload = record.payload
    body = payload.get("body")
    entities = (
        EntityCandidate(
            source=record.ref,
            entity_type="comment",
            # Real Jira sends rich text as a document tree. Only plain bodies
            # are titled here; rendering ADF is enrichment's problem, and the
            # payload is stored verbatim either way.
            title=_clip(body) if isinstance(body, str) else None,
            attrs={"id": payload.get("id")},
        ),
    )

    edges: list[EdgeCandidate] = []
    if record.container is not None:
        edges.append(EdgeCandidate(src=record.ref, dst=record.container, edge_type=BELONGS_TO))
    author = (payload.get("author") or {}).get("accountId")
    if author:
        edges.append(
            EdgeCandidate(src=_person_ref(record, str(author)), dst=record.ref, edge_type=AUTHORED)
        )
    return Extraction(entities=entities, edges=tuple(edges))


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------

EXTRACTORS: dict[str, Extractor] = {
    "slack.channel": extract_slack_channel,
    "slack.message": extract_slack_message,
    "slack.user": extract_person,
    "jira.project": extract_jira_project,
    "jira.issue": extract_jira_issue,
    "jira.comment": extract_jira_comment,
    "jira.user": extract_person,
}

# Source types that are deliberately not entities. Groups are principals: they
# carry permissions and are never the subject of a question, so giving them
# entities would put nodes in the graph that nothing ever cites.
NOT_ENTITIES = frozenset({"slack.group", "jira.group"})


def extract(record: RawRecord) -> Extraction:
    """What one raw record asserts. Deterministic, and safe on unknown types.

    An unrecognised source type yields nothing and logs. It does not raise: the
    raw record is still stored, so adding an extractor later and re-running
    picks it up. That is the whole point of keeping source truth immutable.
    """
    if record.source_type in NOT_ENTITIES:
        return Extraction()

    extractor = EXTRACTORS.get(record.source_type)
    if extractor is None:
        LOG.warning(
            "no extractor for source type; the raw record is kept and will be "
            "picked up by a re-run once one exists",
            extra={"source_type": record.source_type, "source_id": record.source_id},
        )
        return Extraction()
    return extractor(record)


def _sort_key_entity(candidate: EntityCandidate) -> tuple[str, str]:
    return (candidate.source.source_type, candidate.source.source_id)


def _sort_key_edge(candidate: EdgeCandidate) -> tuple[str, str, str, str, str]:
    return (
        candidate.src.source_type,
        candidate.src.source_id,
        candidate.edge_type,
        candidate.dst.source_type,
        candidate.dst.source_id,
    )


def extract_all(records: Iterable[RawRecord]) -> Extraction:
    """Extract a whole corpus, in a stable order.

    Sorted because the result is compared for equality in tests and written in
    batches in P1-RES-2; an unstable order would make both flaky for no reason.
    """
    # Entities are deduplicated by source reference rather than by value:
    # raw_records is already unique on (connector, source_type, source_id), so
    # two candidates for one reference mean two extractors disagreed, and the
    # first one wins deterministically rather than both being written.
    entities: dict[tuple[str, str], EntityCandidate] = {}
    edges: set[EdgeCandidate] = set()

    for record in records:
        result = extract(record)
        for candidate in result.entities:
            entities.setdefault(_sort_key_entity(candidate), candidate)
        edges.update(result.edges)

    return Extraction(
        entities=tuple(sorted(entities.values(), key=_sort_key_entity)),
        edges=tuple(sorted(edges, key=_sort_key_edge)),
    )


def load_raw_records(conn: Connection, connector_id: UUID | None = None) -> list[RawRecord]:
    """Read raw records, optionally for one connector. Reads only."""
    sql = (
        "SELECT id, connector_id, source_type, source_id, payload, "
        "       container_source_type, container_source_id "
        "FROM raw_records"
    )
    params: Sequence[Any] = ()
    if connector_id is not None:
        sql += " WHERE connector_id = %s"
        params = (connector_id,)
    sql += " ORDER BY source_type, source_id"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        RawRecord(
            id=UUID(str(row[0])),
            connector_id=UUID(str(row[1])),
            source_type=str(row[2]),
            source_id=str(row[3]),
            payload=dict(row[4]),
            container=(
                SourceRef(source_type=str(row[5]), source_id=str(row[6]))
                if row[5] is not None
                else None
            ),
        )
        for row in rows
    ]


def extract_connector(conn: Connection, connector_id: UUID | None = None) -> Extraction:
    """Everything one connector's raw records assert. Writes nothing."""
    return extract_all(load_raw_records(conn, connector_id))
