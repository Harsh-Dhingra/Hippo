"""The jobs runtime.

A Postgres table and SELECT ... FOR UPDATE SKIP LOCKED, per STACK.md. Lives in
core rather than in sync or resolver because both of them run jobs, and neither
should have to import the other.

Crash safety is a lease. A worker claims a job by writing an expiry, and a job
whose expiry has passed is reclaimable. Nothing about recovery depends on the
dying process doing anything on its way out, which is the property a
claimed-flag scheme cannot have: kill -9 does not run cleanup code.

Handlers must be idempotent. The runtime guarantees a job is claimed by one
worker at a time and that a crashed job runs again; it cannot guarantee that a
crash happened before rather than after a side effect.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import FrameType
from typing import Any
from uuid import UUID

from prometheus_client import Counter, Histogram
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from core.db import Connection, connect

LOG = logging.getLogger("hippo.jobs")

NOTIFY_CHANNEL = "hippo_jobs"

CLAIMED = Counter("hippo_jobs_claimed_total", "Jobs claimed by a worker.", ("kind",))
SUCCEEDED = Counter("hippo_jobs_succeeded_total", "Jobs that completed.", ("kind",))
RETRIED = Counter("hippo_jobs_retried_total", "Job attempts that failed and will retry.", ("kind",))
DEAD = Counter("hippo_jobs_dead_total", "Jobs that exhausted their attempts.", ("kind",))
RECLAIMED = Counter("hippo_jobs_reclaimed_total", "Jobs reclaimed after a lease expired.")
DURATION = Histogram("hippo_job_duration_seconds", "Handler runtime.", ("kind",))


class Job(BaseModel):
    """A claimed unit of work."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


Handler = Callable[[Job], None]


class LeaseLostError(RuntimeError):
    """The job was reclaimed by the reaper while this worker still held it.

    Means the handler overran its lease. The work may now be running twice.
    """


def enqueue(
    conn: Connection,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    priority: int = 100,
    max_attempts: int = 5,
) -> UUID | None:
    """Add a job. Returns None when `dedupe_key` matches a job already live.

    Deduplication is what makes a scheduler safe to run on a short cadence: the
    ACL fast-lane in P1-SYNC-4 fires every few minutes and must not pile up
    copies of a sync that has not finished yet.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (kind, payload, dedupe_key, priority, max_attempts, run_at) "
            "VALUES (%s, %s, %s, %s, %s, coalesce(%s, now())) "
            "ON CONFLICT (dedupe_key) "
            "  WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running') "
            "DO NOTHING "
            "RETURNING id",
            (kind, Jsonb(dict(payload or {})), dedupe_key, priority, max_attempts, run_at),
        )
        row = cur.fetchone()
    if row is None:
        LOG.info("job deduplicated", extra={"kind": kind, "dedupe_key": dedupe_key})
        return None
    return UUID(str(row[0]))


def claim(
    conn: Connection,
    worker: str,
    kinds: Sequence[str],
    lease_seconds: float,
) -> Job | None:
    """Take the next runnable job of a kind this worker can handle.

    SKIP LOCKED is what lets many workers poll the same table without blocking
    on each other. `kinds` matters because sync and resolver share one queue:
    without it a sync worker would claim resolver work and fail it.
    """
    if not kinds:
        return None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET "
            "    status = 'running', "
            "    attempts = attempts + 1, "
            "    locked_by = %s, "
            "    lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE id = ( "
            "    SELECT id FROM jobs "
            "    WHERE status = 'pending' AND run_at <= now() AND kind = ANY(%s) "
            "    ORDER BY priority, run_at, id "
            "    FOR UPDATE SKIP LOCKED "
            "    LIMIT 1 "
            ") "
            "RETURNING id, kind, payload, attempts, max_attempts",
            (worker, lease_seconds, list(kinds)),
        )
        row = cur.fetchone()

    if row is None:
        return None

    job = Job(
        id=UUID(str(row[0])),
        kind=str(row[1]),
        payload=dict(row[2]),
        attempts=int(row[3]),
        max_attempts=int(row[4]),
    )
    CLAIMED.labels(kind=job.kind).inc()
    LOG.info(
        "job claimed",
        extra={"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts, "worker": worker},
    )
    return job


def complete(conn: Connection, job: Job, worker: str) -> None:
    """Mark a job succeeded. Fenced on the lease this worker still holds."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'succeeded', completed_at = now(), "
            "    lease_expires_at = NULL, locked_by = NULL, last_error = NULL "
            "WHERE id = %s AND status = 'running' AND locked_by = %s",
            (job.id, worker),
        )
        fenced_out = cur.rowcount == 0

    if fenced_out:
        LOG.error(
            "job finished after its lease expired; it may have run twice",
            extra={"job_id": str(job.id), "kind": job.kind, "worker": worker},
        )
        raise LeaseLostError(f"lease on job {job.id} was lost before completion")

    SUCCEEDED.labels(kind=job.kind).inc()
    LOG.info("job succeeded", extra={"job_id": str(job.id), "kind": job.kind})


def backoff_seconds(attempts: int, *, base: float = 1.0, maximum: float = 300.0) -> float:
    """Exponential with jitter. Jitter spreads a thundering herd of retries."""
    ceiling: float = min(maximum, base * float(2 ** max(attempts - 1, 0)))
    return ceiling * random.uniform(0.5, 1.0)


def fail(
    conn: Connection,
    job: Job,
    worker: str,
    error: str,
    *,
    base_backoff: float = 1.0,
    max_backoff: float = 300.0,
) -> str:
    """Record a failed attempt. Returns the resulting status.

    Retries while attempts remain, then dead-letters. Dead is a resting state,
    not a deletion: a dropped job is a job nobody can investigate.
    """
    if job.is_last_attempt:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'dead', completed_at = now(), "
                "    lease_expires_at = NULL, locked_by = NULL, last_error = %s "
                "WHERE id = %s AND status = 'running' AND locked_by = %s",
                (error, job.id, worker),
            )
            fenced_out = cur.rowcount == 0
        if fenced_out:
            raise LeaseLostError(f"lease on job {job.id} was lost before dead-lettering")
        DEAD.labels(kind=job.kind).inc()
        LOG.error(
            "job dead-lettered",
            extra={
                "job_id": str(job.id),
                "kind": job.kind,
                "attempts": job.attempts,
                "error": error,
            },
        )
        return "dead"

    delay = backoff_seconds(job.attempts, base=base_backoff, maximum=max_backoff)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'pending', "
            "    run_at = now() + make_interval(secs => %s), "
            "    lease_expires_at = NULL, locked_by = NULL, last_error = %s "
            "WHERE id = %s AND status = 'running' AND locked_by = %s",
            (delay, error, job.id, worker),
        )
        fenced_out = cur.rowcount == 0
    if fenced_out:
        raise LeaseLostError(f"lease on job {job.id} was lost before retry")

    RETRIED.labels(kind=job.kind).inc()
    LOG.warning(
        "job failed, will retry",
        extra={
            "job_id": str(job.id),
            "kind": job.kind,
            "attempt": job.attempts,
            "retry_in_seconds": round(delay, 3),
            "error": error,
        },
    )
    return "pending"


def reap_expired_leases(conn: Connection) -> tuple[UUID, ...]:
    """Return crashed jobs to the queue. This is the whole crash-recovery story.

    A job whose worker died holds a lease nobody will release, so the expiry is
    what releases it. Jobs that have also exhausted their attempts go straight
    to the dead letter rather than looping.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET "
            "    status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END, "
            "    completed_at = CASE WHEN attempts >= max_attempts THEN now() END, "
            "    lease_expires_at = NULL, "
            "    locked_by = NULL, "
            "    last_error = 'worker lease expired; reclaimed' "
            "WHERE status = 'running' AND lease_expires_at < now() "
            "RETURNING id",
            (),
        )
        rows = cur.fetchall()

    reclaimed = tuple(UUID(str(row[0])) for row in rows)
    if reclaimed:
        RECLAIMED.inc(len(reclaimed))
        LOG.warning(
            "reclaimed jobs from expired leases",
            extra={"count": len(reclaimed), "job_ids": [str(i) for i in reclaimed]},
        )
    return reclaimed


def queue_depth(conn: Connection) -> dict[str, int]:
    """Count by status. Feeds the queue-depth metric in P2-OBS-1."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM jobs GROUP BY status")
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}


@dataclass(frozen=True)
class WorkerConfig:
    """Worker tuning.

    lease_seconds must exceed the slowest handler this worker runs. A handler
    that overruns its lease gets its job reclaimed underneath it; the fencing in
    complete() and fail() turns that into a loud LeaseLostError rather than a
    silent double-write.
    """

    name: str
    lease_seconds: float = 300.0
    poll_interval: float = 1.0
    base_backoff: float = 1.0
    max_backoff: float = 300.0


def default_worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    """Polls for work, runs handlers, and sleeps on LISTEN between rounds."""

    def __init__(
        self,
        dsn: str,
        handlers: Mapping[str, Handler],
        config: WorkerConfig | None = None,
    ) -> None:
        self._dsn = dsn
        self._handlers = dict(handlers)
        self._config = config if config is not None else WorkerConfig(name=default_worker_name())

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def run_once(self, conn: Connection) -> bool:
        """Claim and run at most one job. False when there was nothing to do."""
        job = claim(conn, self._config.name, self.kinds, self._config.lease_seconds)
        if job is None:
            return False

        started = time.monotonic()
        try:
            self._handlers[job.kind](job)
        except Exception as exc:
            fail(
                conn,
                job,
                self._config.name,
                f"{type(exc).__name__}: {exc}",
                base_backoff=self._config.base_backoff,
                max_backoff=self._config.max_backoff,
            )
        else:
            complete(conn, job, self._config.name)
        finally:
            DURATION.labels(kind=job.kind).observe(time.monotonic() - started)
        return True

    def drain(self, conn: Connection, limit: int | None = None) -> int:
        """Run jobs until the queue is empty. Returns how many ran."""
        done = 0
        while limit is None or done < limit:
            if not self.run_once(conn):
                break
            done += 1
        return done

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """The worker loop. Polling is the correctness mechanism; the LISTEN is
        only there to cut idle latency, so a missed notification costs a poll
        interval and never a job."""
        halt = stop if stop is not None else threading.Event()
        with connect(self._dsn, autocommit=True) as conn:
            conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
            LOG.info(
                "worker started",
                extra={"worker": self._config.name, "kinds": list(self.kinds)},
            )
            while not halt.is_set():
                reap_expired_leases(conn)
                while not halt.is_set() and self.run_once(conn):
                    pass
                self._sleep_until_work(conn)
        LOG.info("worker stopped", extra={"worker": self._config.name})

    def run_until_signalled(self) -> None:
        """run_forever plus graceful SIGINT/SIGTERM. SIGKILL is not handled
        here, by definition; the lease is what covers it."""
        halt = threading.Event()

        def _stop(_signum: int, _frame: FrameType | None) -> None:
            halt.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        self.run_forever(halt)

    def _sleep_until_work(self, conn: Connection) -> None:
        for _ in conn.notifies(timeout=self._config.poll_interval, stop_after=1):
            break
