"""Reading and keeping the audit log.

Migration 018 makes action_events append-only and writes it from a trigger, so
there is nothing here that records an event — that is the point. What is here
is the two things an operator does with a log: read it, and eventually stop
keeping it.

**Retention is deliberate, not automatic.** No service role holds DELETE on
action_events, so purging runs as the owner and is something a scheduled task
does on purpose. An audit log the application can trim on its own is one an
attacker can trim on its own, and the difference between those two sentences is
smaller than it looks.

**Export exists because an audit that cannot leave is not evidence.** A
compliance reviewer, an incident write-up and a regulator all want a file
rather than a screen, and building that later always means building it in a
hurry.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from core.db import Connection

LOG = logging.getLogger("hippo.audit")

# Long enough that a quarterly review still finds what it needs, and short
# enough to be a decision rather than a default of "forever". An operator who
# needs a different number sets one; an operator who never thinks about it gets
# something defensible.
DEFAULT_RETENTION = timedelta(days=365)

EXPORT_COLUMNS = (
    "at",
    "action_id",
    "from_status",
    "to_status",
    "actor",
    "actor_policy",
    "action_type",
    "risk_class",
    "summary",
)


class AuditEvent(BaseModel):
    """One transition in the life of an action."""

    model_config = ConfigDict(frozen=True)

    id: int
    action_id: UUID
    at: datetime
    from_status: str | None
    to_status: str
    actor: UUID | None
    actor_policy: str | None
    action_type: str | None
    risk_class: str | None
    summary: str | None
    snapshot: dict[str, Any]

    @property
    def decided_by(self) -> str:
        """Who is responsible, in words rather than in three nullable columns.

        The distinction a policy approval exists to preserve: "a policy" is not
        "a person", and an export that rendered both as an id would lose the
        one fact the audit log was designed to keep.
        """
        if self.actor_policy:
            return f"policy: {self.actor_policy}"
        if self.actor:
            return str(self.actor)
        return "the system"


def events_for(
    conn: Connection,
    principal_id: UUID,
    *,
    status: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[AuditEvent]:
    """The log, filtered, and scoped to the reader.

    An audit log of other people's actions is a list of things they can see, so
    this returns yours — the same rule traces and actions already follow.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM my_action_events(%s, %s, %s, %s)",
            (principal_id, status, since, limit),
        )
        columns = [description.name for description in cur.description or []]
        return [
            AuditEvent.model_validate(dict(zip(columns, row, strict=True)))
            for row in cur.fetchall()
        ]


def to_csv(events: Sequence[AuditEvent]) -> str:
    """A spreadsheet, because that is what a reviewer opens.

    The snapshot is left out: a payload with a newline in it turns a CSV into a
    puzzle, and anyone who needs the payload wants JSON anyway.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([*EXPORT_COLUMNS, "decided_by"])
    for event in events:
        writer.writerow(
            [
                event.at.isoformat(),
                str(event.action_id),
                event.from_status or "",
                event.to_status,
                str(event.actor) if event.actor else "",
                event.actor_policy or "",
                event.action_type or "",
                event.risk_class or "",
                event.summary or "",
                event.decided_by,
            ]
        )
    return buffer.getvalue()


def to_jsonl(events: Sequence[AuditEvent]) -> Iterator[str]:
    """One event per line, snapshot included.

    Streamed rather than assembled: an export is exactly the request most
    likely to be large, and holding a year of it in memory to hand back one
    string is how an audit endpoint becomes an outage.
    """
    for event in events:
        yield (
            json.dumps(
                {
                    "at": event.at.isoformat(),
                    "action_id": str(event.action_id),
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "actor": str(event.actor) if event.actor else None,
                    "actor_policy": event.actor_policy,
                    "decided_by": event.decided_by,
                    "snapshot": event.snapshot,
                },
                separators=(",", ":"),
            )
            + "\n"
        )


def purge(conn: Connection, older_than: timedelta = DEFAULT_RETENTION) -> int:
    """Delete events past the retention window.

    Runs as the owner, because no service role holds DELETE here. That is the
    whole retention story: keeping the log is the default, and removing any of
    it is something a person set up on purpose.

    Events belonging to actions that are still live are kept regardless of age.
    An action pending for fourteen months is unusual and is exactly the one
    whose history somebody will want.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM action_events e USING actions a "
            "WHERE a.id = e.action_id "
            "  AND e.at < now() - %s::interval "
            "  AND a.status IN ('executed', 'rolled_back', 'declined', 'failed')",
            (older_than,),
        )
        removed = cur.rowcount

    if removed:
        LOG.warning(
            "audit events purged past retention",
            extra={"events": removed, "older_than_days": older_than.days},
        )
    return removed
