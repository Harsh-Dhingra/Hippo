"""The Memory Timeline.

PROJECT.md: "Reconstruct the causal/temporal chain around any entity — pricing
change, Slack thread, PR, deploy, complaint, ticket, fix — from timestamped
entities and edges."

This is the thing the graph was built for. Retrieval answers "what is relevant
to this question"; a timeline answers "what happened around this thing, in
order", which is a different question and one that a vector index cannot
express at all. It needs edges and it needs time, and having built both, the
query is short.

**Time is what the source said.** `entities.occurred_at`, not `created_at` —
the latter is when the resolver first saw the record, and in a freshly synced
workspace every entity shares one. A timeline built on it would draw a flat
line at import time and call it history.

**Undated entities are context, not events.** People, channels and projects
carry no time of their own, and inventing one would be inventing history. They
sort last and are labelled as context, because they explain the chain rather
than belonging to it.

**Permission is the same predicate as everywhere else.** A timeline is a new
way to see what exists, so it would be a new way to leak — except that it reuses
the ACL closure the retrieval filter already uses for graph expansion. An entry
appears in your timeline exactly when it could appear in your answers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent.links import ConnectorDirectory, deep_link
from core.db import Connection

LOG = logging.getLogger("hippo.agent.timeline")

# How an edge reads in a sentence. The timeline says "a reply to the thread"
# rather than "related to the thread", because the edge type is the only thing
# that makes a chain causal rather than merely chronological.
RELATION = {
    "authored": "written by",
    "belongs_to": "in",
    "replies_to": "a reply in",
    "mentions": "mentions",
    "assigned_to": "assigned to",
}


class Moment(BaseModel):
    """One thing that happened, or one thing that explains it."""

    model_config = ConfigDict(frozen=True)

    entity_id: UUID
    entity_type: str
    title: str | None
    occurred_at: datetime | None
    hops: int
    via: str | None
    url: str | None
    source_type: str | None

    @property
    def is_context(self) -> bool:
        """No time of its own. A person or a channel is why the chain hangs
        together, not a step in it."""
        return self.occurred_at is None

    @property
    def relation(self) -> str:
        if self.via is None:
            return "the subject"
        return RELATION.get(self.via, self.via.replace("_", " "))


class Timeline(BaseModel):
    """The chain around one entity."""

    model_config = ConfigDict(frozen=True)

    subject: UUID
    moments: tuple[Moment, ...]

    @property
    def events(self) -> tuple[Moment, ...]:
        return tuple(moment for moment in self.moments if not moment.is_context)

    @property
    def context(self) -> tuple[Moment, ...]:
        return tuple(moment for moment in self.moments if moment.is_context)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        """First and last dated moment, or None when nothing is dated.

        Worth having as its own property: a timeline covering four minutes and
        one covering four months read very differently, and the header should
        say which it is.
        """
        dated = [moment.occurred_at for moment in self.events if moment.occurred_at]
        return (min(dated), max(dated)) if dated else None


def build(
    conn: Connection,
    principal_id: UUID,
    entity_id: UUID,
    *,
    hops: int = 2,
    limit: int = 100,
    directory: ConnectorDirectory | None = None,
) -> Timeline:
    """Walk out from one entity and put what is reachable in order.

    Two hops by default. One shows only what touches the subject directly,
    which is usually the thread it is in and nothing else; three tends to reach
    the whole workspace through a shared channel, and a timeline that includes
    everything is a timeline about nothing.
    """
    resolved = directory or ConnectorDirectory()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id, entity_type, title, occurred_at, hops, via, "
            "       connector_id, source_type, source_id "
            "FROM timeline(%s, %s, %s, %s)",
            (principal_id, entity_id, hops, limit),
        )
        rows = cur.fetchall()

    moments = tuple(_moment(row, resolved) for row in rows)
    LOG.info(
        "timeline built",
        extra={
            "subject": str(entity_id),
            "moments": len(moments),
            "dated": sum(1 for moment in moments if not moment.is_context),
        },
    )
    return Timeline(subject=entity_id, moments=moments)


def _moment(row: Any, directory: ConnectorDirectory) -> Moment:
    connector_id = None if row[6] is None else UUID(str(row[6]))
    source_type = None if row[7] is None else str(row[7])
    source_id = None if row[8] is None else str(row[8])
    return Moment(
        entity_id=UUID(str(row[0])),
        entity_type=str(row[1]),
        title=None if row[2] is None else str(row[2]),
        occurred_at=row[3],
        hops=int(row[4]),
        via=None if row[5] is None else str(row[5]),
        # A timeline entry nobody can click through to is an assertion rather
        # than evidence, which is the same reason citations carry links.
        url=deep_link(directory.get(connector_id), source_type, source_id),
        source_type=source_type,
    )
