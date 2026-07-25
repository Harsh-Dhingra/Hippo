"""The P1-CORE-4 done-condition: kill -9 a worker mid-job.

This kills a real operating system process with SIGKILL. Simulating a crash by
raising an exception would prove nothing, because the whole question is what
happens when no cleanup code runs at all.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from core.db import Connection, connect
from core.jobs import Job, Worker, WorkerConfig, enqueue, reap_expired_leases
from tests.crash_worker import KIND
from tests.graceful_worker import KIND as GRACEFUL_KIND

pytestmark = pytest.mark.requires_db

REPO_ROOT = Path(__file__).resolve().parent.parent
LEASE_SECONDS = 2.0


@pytest.fixture
def crash_db(migrated: Connection) -> Connection:
    """A marker table the killed process writes to, so the parent can tell
    exactly how far it got."""
    migrated.execute(
        "CREATE TABLE crash_marks ("
        "  id serial PRIMARY KEY,"
        "  job_id uuid NOT NULL,"
        "  phase text NOT NULL,"
        "  worker text NOT NULL,"
        "  at timestamptz NOT NULL DEFAULT now())"
    )
    return migrated


@pytest.fixture
def crash_worker(db_dsn: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.crash_worker", db_dsn, str(LEASE_SECONDS)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _wait_for(conn: Connection, sql: str, params: tuple[object, ...], timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is not None and int(row[0]) > 0:
            return int(row[0])
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {sql} {params}")


def _status(conn: Connection, job_id: UUID) -> tuple[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    return str(row[0]), int(row[1])


def test_killed_worker_job_reruns_exactly_once(
    crash_db: Connection, db_dsn: str, crash_worker: subprocess.Popen[bytes]
) -> None:
    """The done-condition, end to end.

    A worker claims a job, is SIGKILLed while holding it, and the job comes back
    and completes once. Not zero times, which would mean work is lost on any
    crash, and not twice concurrently, which would mean the lease is decoration.
    """
    job_id = enqueue(crash_db, KIND, {"n": 1}, max_attempts=5)
    assert job_id is not None

    # Wait until the doomed worker is genuinely inside the handler.
    _wait_for(
        crash_db,
        "SELECT count(*) FROM crash_marks WHERE job_id = %s AND phase = 'started'",
        (job_id,),
    )
    assert _status(crash_db, job_id) == ("running", 1)

    # While the lease is held, nobody else may take the job.
    assert Worker(db_dsn, {KIND: lambda _job: None}).run_once(crash_db) is False, (
        "a leased job must not be claimable by a second worker"
    )

    os.kill(crash_worker.pid, signal.SIGKILL)
    crash_worker.wait(timeout=10)
    assert crash_worker.returncode == -signal.SIGKILL

    # Nothing was cleaned up: the job is still marked running, holding a lease
    # that no process will ever release.
    assert _status(crash_db, job_id) == ("running", 1)

    # Once the lease expires, the reaper is what makes the job runnable again.
    time.sleep(LEASE_SECONDS + 0.5)
    assert reap_expired_leases(crash_db) == (job_id,)
    assert _status(crash_db, job_id) == ("pending", 1)

    def finish(job: Job) -> None:
        with connect(db_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO crash_marks (job_id, phase, worker) "
                "VALUES (%s, 'completed', 'recovery')",
                (job.id,),
            )

    recovered = Worker(
        db_dsn, {KIND: finish}, WorkerConfig(name="recovery", lease_seconds=30)
    ).run_once(crash_db)

    assert recovered is True
    assert _status(crash_db, job_id) == ("succeeded", 2)

    with crash_db.cursor() as cur:
        cur.execute(
            "SELECT phase, count(*) FROM crash_marks WHERE job_id = %s "
            "GROUP BY phase ORDER BY phase",
            (job_id,),
        )
        assert cur.fetchall() == [("completed", 1), ("started", 1)], (
            "the job ran again after the crash, and its side effect happened once"
        )


def test_killed_worker_does_not_lose_other_queued_work(
    crash_db: Connection, db_dsn: str, crash_worker: subprocess.Popen[bytes]
) -> None:
    """A crash takes down one job, not the queue."""
    doomed = enqueue(crash_db, KIND, {"n": 1})
    assert doomed is not None
    _wait_for(
        crash_db,
        "SELECT count(*) FROM crash_marks WHERE job_id = %s AND phase = 'started'",
        (doomed,),
    )

    survivors = [enqueue(crash_db, "other.kind", {"n": n}) for n in range(3)]

    os.kill(crash_worker.pid, signal.SIGKILL)
    crash_worker.wait(timeout=10)

    ran: list[UUID] = []
    worker = Worker(
        db_dsn,
        {"other.kind": lambda job: ran.append(job.id)},
        WorkerConfig(name="survivor", lease_seconds=30),
    )
    worker.drain(crash_db)

    assert sorted(ran, key=str) == sorted([s for s in survivors if s is not None], key=str)
    assert _status(crash_db, doomed)[0] == "running", "the doomed job still holds its lease"


def test_sigterm_shuts_a_worker_down_cleanly(crash_db: Connection, db_dsn: str) -> None:
    """The counterpart to SIGKILL. A worker asked to stop finishes and exits
    zero, so a rolling restart does not depend on lease expiry to recover."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.graceful_worker", db_dsn],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        job_id = enqueue(crash_db, GRACEFUL_KIND, {"n": 1})
        assert job_id is not None
        _wait_for(
            crash_db,
            "SELECT count(*) FROM crash_marks WHERE job_id = %s AND phase = 'started'",
            (job_id,),
        )

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0, "a signalled worker must exit cleanly"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert _status(crash_db, job_id) == ("succeeded", 1)
    assert reap_expired_leases(crash_db) == (), "a clean exit leaves no stranded lease"


def test_an_idle_worker_wakes_on_notify(crash_db: Connection, db_dsn: str) -> None:
    """The LISTEN wakeup, end to end: enqueue after the worker has gone idle and
    it should pick the job up well inside a poll interval."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.graceful_worker", db_dsn],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1.0)  # let it finish its first pass and block on LISTEN
        job_id = enqueue(crash_db, GRACEFUL_KIND, {"n": 1})
        assert job_id is not None

        _wait_for(
            crash_db,
            "SELECT count(*) FROM crash_marks WHERE job_id = %s",
            (job_id,),
            timeout=10,
        )
    finally:
        process.send_signal(signal.SIGTERM)
        if process.wait(timeout=20) is None:  # pragma: no cover - defensive
            process.kill()

    assert _status(crash_db, job_id) == ("succeeded", 1)


def test_a_job_that_keeps_crashing_eventually_dead_letters(crash_db: Connection) -> None:
    """Reclaiming must not become an infinite loop. Attempts still count."""
    job_id = enqueue(crash_db, KIND, {"n": 1}, max_attempts=2)
    assert job_id is not None

    for _ in range(2):
        crash_db.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
            "locked_by = 'ghost', lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (job_id,),
        )
        reap_expired_leases(crash_db)

    assert _status(crash_db, job_id) == ("dead", 2)
