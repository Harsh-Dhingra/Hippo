"""When each stream runs.

ARCHITECTURE §8: "ACL streams run at a higher frequency than content streams. A
revoked permission must propagate within minutes, not at the next nightly sync.
v0 target: ACL sync ≤ 5 min, content sync ≤ 15 min." §11 lists ACL propagation
under the security story rather than under performance, which is the right
place for it: a stale permission is not a slow feature, it is a wrong answer.

So the cadences are not one number with a comment. ACLs get their own schedule,
their own job kind and their own worst case, and the worst case is arithmetic
rather than a hope:

    staleness ≤ interval + however long one run takes

That is why the default interval is four minutes and not five. A five-minute
cadence cannot keep a five-minute promise, because the run itself is not
instantaneous, and the difference between those two numbers is exactly the kind
of thing that goes unnoticed until someone checks whether a revocation actually
propagated.

Scheduling is separate from running. This module decides what is due and
enqueues it; the jobs runtime owns leases, retries and the dead letter, and a
second scheduler on another node cannot double-enqueue because the dedupe key
is per connector and stream.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from prometheus_client import Gauge
from pydantic import BaseModel, ConfigDict, Field

from core.db import Connection
from core.jobs import enqueue

LOG = logging.getLogger("hippo.sync.scheduler")

# The promise, in one place. ARCHITECTURE §8 and §11 both state it; this is the
# constant the measurement in the tests is against.
ACL_PROPAGATION_TARGET = timedelta(minutes=5)
CONTENT_TARGET = timedelta(minutes=15)

ACL_STREAM = "acls"
JOB_KIND = "sync.stream"


class Cadence(BaseModel):
    """How often each stream runs, in seconds."""

    model_config = ConfigDict(frozen=True)

    # Four minutes, not five: see the module docstring. The remainder is the
    # budget for the run itself.
    acls: int = Field(default=240, ge=10)
    identities: int = Field(default=900, ge=10)
    content: int = Field(default=900, ge=10)

    def interval(self, stream: str) -> timedelta:
        return timedelta(seconds=int(getattr(self, stream)))

    def worst_case_staleness(self, stream: str, run_duration: timedelta) -> timedelta:
        """The longest a source change can go unreflected.

        One full interval of waiting plus one run. Stated as a function rather
        than a comment so a test can measure the run and check the sum.
        """
        return self.interval(stream) + run_duration

    def keeps_the_acl_promise(self, run_duration: timedelta) -> bool:
        return self.worst_case_staleness(ACL_STREAM, run_duration) <= ACL_PROPAGATION_TARGET


class DueStream(BaseModel):
    """A stream that should run now."""

    model_config = ConfigDict(frozen=True)

    connector_id: UUID
    stream: str
    last_synced_at: datetime | None

    @property
    def dedupe_key(self) -> str:
        return f"{JOB_KIND}:{self.connector_id}:{self.stream}"


def due_streams(
    conn: Connection,
    cadence: Cadence | None = None,
    *,
    streams: Sequence[str] = ("identities", "content", ACL_STREAM),
) -> list[DueStream]:
    """Every connector stream whose interval has elapsed.

    A stream that has never run is due immediately: a connector added between
    two ticks should not wait a full interval before anyone can see anything.

    `now` comes from the database rather than from Python, so two schedulers on
    machines whose clocks disagree still make the same decision.
    """
    resolved = cadence or Cadence()
    due: list[DueStream] = []

    with conn.cursor() as cur:
        for stream in streams:
            cur.execute(
                "SELECT c.id, s.last_synced_at "
                "FROM connectors c "
                "LEFT JOIN sync_state s ON s.connector_id = c.id AND s.stream = %s "
                "WHERE s.last_synced_at IS NULL "
                "   OR s.last_synced_at <= now() - %s::interval",
                (stream, resolved.interval(stream)),
            )
            due.extend(
                DueStream(connector_id=UUID(str(row[0])), stream=stream, last_synced_at=row[1])
                for row in cur.fetchall()
            )

    return due


def enqueue_due(conn: Connection, cadence: Cadence | None = None) -> list[UUID]:
    """Enqueue a job for every due stream.

    ACLs are enqueued at a lower priority number, so when the queue is backed
    up a revocation goes out before a batch of new messages. Under load is
    exactly when that ordering matters.

    Deduped per connector and stream, so a run that overruns its interval does
    not accumulate a queue of identical work behind it.
    """
    job_ids: list[UUID] = []
    for item in due_streams(conn, cadence):
        job_id = enqueue(
            conn,
            JOB_KIND,
            payload={"connector_id": str(item.connector_id), "stream": item.stream},
            dedupe_key=item.dedupe_key,
            priority=10 if item.stream == ACL_STREAM else 100,
        )
        if job_id is not None:
            job_ids.append(job_id)

    if job_ids:
        LOG.info("enqueued due syncs", extra={"jobs": len(job_ids)})
    return job_ids


# ---------------------------------------------------------------------------
# Is the promise being kept right now?
# ---------------------------------------------------------------------------

ACL_STALENESS = Gauge(
    "hippo_acl_staleness_seconds",
    "Seconds since this connector's ACLs were last refreshed.",
    ("connector", "kind"),
)


class Staleness(BaseModel):
    """How out of date one connector's permissions are."""

    model_config = ConfigDict(frozen=True)

    connector_id: UUID
    kind: str
    seconds: float | None

    @property
    def within_target(self) -> bool:
        """Never synced counts as stale. A connector whose ACLs have not landed
        yet is not one whose permissions are up to date; it is one whose
        permissions are unknown, and the safe reading of unknown is stale."""
        if self.seconds is None:
            return False
        return self.seconds <= ACL_PROPAGATION_TARGET.total_seconds()


def acl_staleness(conn: Connection) -> list[Staleness]:
    """Per connector, how long since ACLs last landed.

    A test can prove the fast lane propagates a revocation. Only this can say
    whether it is propagating them today — an expired Slack token freezes
    permissions at their last known state, and nothing else in the system would
    notice that the security story had quietly stopped being true.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.kind, extract(epoch FROM now() - s.last_synced_at) "
            "FROM connectors c "
            "LEFT JOIN sync_state s ON s.connector_id = c.id AND s.stream = %s "
            "ORDER BY c.id",
            (ACL_STREAM,),
        )
        rows = cur.fetchall()

    report = [
        Staleness(
            connector_id=UUID(str(row[0])),
            kind=str(row[1]),
            seconds=None if row[2] is None else float(row[2]),
        )
        for row in rows
    ]
    for item in report:
        # A never-synced connector reports infinity rather than zero. Zero is
        # what "just synced" looks like, and a missing measurement must not be
        # mistaken for the best possible one.
        ACL_STALENESS.labels(connector=str(item.connector_id), kind=item.kind).set(
            float("inf") if item.seconds is None else item.seconds
        )
    return report
