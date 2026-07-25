"""The jobs runtime: claiming, retrying, dead-lettering, and not double-running."""

import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from core.db import Connection, connect
from core.jobs import (
    NOTIFY_CHANNEL,
    Job,
    LeaseLostError,
    Worker,
    WorkerConfig,
    backoff_seconds,
    claim,
    complete,
    enqueue,
    fail,
    queue_depth,
    reap_expired_leases,
)

pytestmark = pytest.mark.requires_db

WORKER = "test-worker"


def status_of(conn: Connection, job_id: UUID) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    assert row is not None
    return str(row[0])


def row_of(conn: Connection, job_id: UUID) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, run_at, last_error, locked_by, lease_expires_at, "
            "completed_at FROM jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
    assert row is not None
    keys = (
        "status",
        "attempts",
        "run_at",
        "last_error",
        "locked_by",
        "lease_expires_at",
        "completed_at",
    )
    return dict(zip(keys, row, strict=True))


# ---------------------------------------------------------------------------
# Enqueue.
# ---------------------------------------------------------------------------


def test_enqueue_creates_a_pending_job(migrated: Connection) -> None:
    job_id = enqueue(migrated, "sync.acls", {"connector": "slack"})

    assert job_id is not None
    row = row_of(migrated, job_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_enqueue_defaults_to_an_empty_payload(migrated: Connection) -> None:
    job_id = enqueue(migrated, "sync.acls")
    assert job_id is not None

    with migrated.cursor() as cur:
        cur.execute("SELECT payload FROM jobs WHERE id = %s", (job_id,))
        assert cur.fetchone() == ({},)


def test_dedupe_key_prevents_a_second_live_copy(migrated: Connection) -> None:
    """The ACL fast-lane fires every few minutes and must not pile up copies."""
    first = enqueue(migrated, "sync.acls", dedupe_key="slack:acls")
    second = enqueue(migrated, "sync.acls", dedupe_key="slack:acls")

    assert first is not None
    assert second is None

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone() == (1,)


def test_dedupe_key_is_reusable_once_the_job_finishes(migrated: Connection) -> None:
    first = enqueue(migrated, "sync.acls", dedupe_key="slack:acls")
    assert first is not None
    job = claim(migrated, WORKER, ["sync.acls"], 60)
    assert job is not None
    complete(migrated, job, WORKER)

    assert enqueue(migrated, "sync.acls", dedupe_key="slack:acls") is not None


def test_jobs_without_a_dedupe_key_never_collide(migrated: Connection) -> None:
    ids = [enqueue(migrated, "sync.acls") for _ in range(5)]
    assert len(set(ids)) == 5


def test_enqueue_can_schedule_for_later(migrated: Connection) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    job_id = enqueue(migrated, "sync.acls", run_at=later)
    assert job_id is not None

    assert claim(migrated, WORKER, ["sync.acls"], 60) is None, "not runnable yet"


# ---------------------------------------------------------------------------
# Claiming.
# ---------------------------------------------------------------------------


def test_claim_returns_none_on_an_empty_queue(migrated: Connection) -> None:
    assert claim(migrated, WORKER, ["sync.acls"], 60) is None


def test_claim_marks_running_and_counts_the_attempt(migrated: Connection) -> None:
    job_id = enqueue(migrated, "sync.acls")
    assert job_id is not None

    job = claim(migrated, WORKER, ["sync.acls"], 60)

    assert job is not None
    assert job.id == job_id
    assert job.attempts == 1
    row = row_of(migrated, job_id)
    assert row["status"] == "running"
    assert row["locked_by"] == WORKER
    assert row["lease_expires_at"] is not None


def test_claim_only_takes_kinds_the_worker_handles(migrated: Connection) -> None:
    """Sync and resolver share one queue; neither may take the other's work."""
    enqueue(migrated, "resolver.extract")

    assert claim(migrated, WORKER, ["sync.acls"], 60) is None
    assert claim(migrated, WORKER, ["resolver.extract"], 60) is not None


def test_claim_with_no_kinds_takes_nothing(migrated: Connection) -> None:
    enqueue(migrated, "sync.acls")
    assert claim(migrated, WORKER, [], 60) is None


def test_claim_respects_priority_then_run_at(migrated: Connection) -> None:
    low = enqueue(migrated, "k", {"tag": "low"}, priority=200)
    high = enqueue(migrated, "k", {"tag": "high"}, priority=1)

    first = claim(migrated, WORKER, ["k"], 60)
    second = claim(migrated, WORKER, ["k"], 60)

    assert first is not None
    assert second is not None
    assert (first.id, second.id) == (high, low)


def test_a_running_job_cannot_be_claimed_again(migrated: Connection) -> None:
    enqueue(migrated, "k")

    assert claim(migrated, WORKER, ["k"], 60) is not None
    assert claim(migrated, "other-worker", ["k"], 60) is None


def test_concurrent_workers_never_claim_the_same_job(db_dsn: str, migrated: Connection) -> None:
    """The SKIP LOCKED property, under genuine contention."""
    expected = {enqueue(migrated, "k", {"n": n}) for n in range(40)}
    claimed: list[UUID] = []
    errors_seen: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def drain() -> None:
        try:
            with connect(db_dsn, autocommit=True) as conn:
                start.wait(timeout=10)
                while True:
                    job = claim(conn, f"w-{threading.get_ident()}", ["k"], 60)
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
        except BaseException as exc:
            errors_seen.append(exc)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors_seen == []
    assert len(claimed) == len(set(claimed)), "a job was claimed twice"
    assert set(claimed) == expected


# ---------------------------------------------------------------------------
# Completion, retry, dead letter.
# ---------------------------------------------------------------------------


def test_complete_marks_succeeded_and_clears_the_lease(migrated: Connection) -> None:
    enqueue(migrated, "k")
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None

    complete(migrated, job, WORKER)

    row = row_of(migrated, job.id)
    assert row["status"] == "succeeded"
    assert row["locked_by"] is None
    assert row["lease_expires_at"] is None
    assert row["completed_at"] is not None


def test_failure_schedules_a_retry_in_the_future(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=3)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None

    assert fail(migrated, job, WORKER, "boom") == "pending"

    row = row_of(migrated, job.id)
    assert row["status"] == "pending"
    assert row["last_error"] == "boom"
    run_at = row["run_at"]
    assert isinstance(run_at, datetime)
    assert run_at > datetime.now(UTC), "backoff must push the job into the future"


def test_a_retried_job_is_not_immediately_claimable(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=3)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    fail(migrated, job, WORKER, "boom", base_backoff=30)

    assert claim(migrated, WORKER, ["k"], 60) is None


def test_the_last_attempt_dead_letters(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=1)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None

    assert fail(migrated, job, WORKER, "boom") == "dead"

    row = row_of(migrated, job.id)
    assert row["status"] == "dead"
    assert row["last_error"] == "boom"
    assert row["completed_at"] is not None


def test_a_dead_job_is_never_claimed_again(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=1)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    fail(migrated, job, WORKER, "boom")

    assert claim(migrated, WORKER, ["k"], 60) is None


def test_dead_jobs_are_kept_not_deleted(migrated: Connection) -> None:
    """Dead-letter over silent drop: a dropped job is one nobody can debug."""
    enqueue(migrated, "k", max_attempts=1)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    fail(migrated, job, WORKER, "the reason")

    with migrated.cursor() as cur:
        cur.execute("SELECT last_error FROM jobs WHERE id = %s", (job.id,))
        assert cur.fetchone() == ("the reason",)


@pytest.mark.parametrize("attempts", [1, 2, 3, 8, 40])
def test_backoff_grows_and_is_capped(attempts: int) -> None:
    delay = backoff_seconds(attempts, base=1.0, maximum=300.0)
    assert 0 < delay <= 300.0


def test_backoff_is_jittered() -> None:
    """Identical retry storms must not synchronise."""
    delays = {backoff_seconds(6) for _ in range(50)}
    assert len(delays) > 1


# ---------------------------------------------------------------------------
# Fencing: a worker that lost its lease must not report a result.
# ---------------------------------------------------------------------------


def test_completing_after_losing_the_lease_raises(migrated: Connection) -> None:
    enqueue(migrated, "k")
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET status='pending', locked_by=NULL, lease_expires_at=NULL WHERE id=%s",
        (job.id,),
    )

    with pytest.raises(LeaseLostError):
        complete(migrated, job, WORKER)


def test_failing_after_losing_the_lease_raises(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=5)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET status='pending', locked_by=NULL, lease_expires_at=NULL WHERE id=%s",
        (job.id,),
    )

    with pytest.raises(LeaseLostError):
        fail(migrated, job, WORKER, "boom")


def test_dead_lettering_after_losing_the_lease_raises(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=1)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET status='pending', locked_by=NULL, lease_expires_at=NULL WHERE id=%s",
        (job.id,),
    )

    with pytest.raises(LeaseLostError):
        fail(migrated, job, WORKER, "boom")


def test_another_worker_cannot_complete_your_job(migrated: Connection) -> None:
    enqueue(migrated, "k")
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None

    with pytest.raises(LeaseLostError):
        complete(migrated, job, "impostor")

    assert status_of(migrated, job.id) == "running"


# ---------------------------------------------------------------------------
# The reaper.
# ---------------------------------------------------------------------------


def test_reaper_ignores_live_leases(migrated: Connection) -> None:
    enqueue(migrated, "k")
    claim(migrated, WORKER, ["k"], 600)

    assert reap_expired_leases(migrated) == ()


def test_reaper_returns_expired_jobs_to_pending(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=5)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job.id,)
    )

    assert reap_expired_leases(migrated) == (job.id,)
    row = row_of(migrated, job.id)
    assert row["status"] == "pending"
    assert row["locked_by"] is None
    assert row["attempts"] == 1, "the attempt still counts"


def test_reaper_dead_letters_when_attempts_are_exhausted(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=1)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job.id,)
    )

    reap_expired_leases(migrated)

    assert status_of(migrated, job.id) == "dead"


def test_a_reaped_job_is_claimable_again(migrated: Connection) -> None:
    enqueue(migrated, "k", max_attempts=5)
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    migrated.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job.id,)
    )
    reap_expired_leases(migrated)

    again = claim(migrated, "second-worker", ["k"], 60)

    assert again is not None
    assert again.id == job.id
    assert again.attempts == 2


# ---------------------------------------------------------------------------
# Schema guarantees.
# ---------------------------------------------------------------------------


def test_a_running_job_must_hold_a_lease(migrated: Connection) -> None:
    """Without this, a running job with no expiry would be unreclaimable."""
    with pytest.raises(errors.CheckViolation, match="running_holds_a_lease"):
        migrated.execute("INSERT INTO jobs (kind, status) VALUES ('k', 'running')")


def test_unknown_job_status_is_rejected(migrated: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="status_known"):
        migrated.execute("INSERT INTO jobs (kind, status) VALUES ('k', 'zombie')")


def test_insert_notifies_listeners(migrated: Connection, db_dsn: str) -> None:
    """LISTEN/NOTIFY is the wakeup that keeps an idle worker from waiting out a
    full poll interval."""
    with connect(db_dsn, autocommit=True) as listener:
        listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
        enqueue(migrated, "sync.acls")

        received = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]

    assert received == ["sync.acls"]


def test_queue_depth_counts_by_status(migrated: Connection) -> None:
    enqueue(migrated, "k")
    enqueue(migrated, "k")
    job = claim(migrated, WORKER, ["k"], 60)
    assert job is not None
    complete(migrated, job, WORKER)

    assert queue_depth(migrated) == {"pending": 1, "succeeded": 1}


# ---------------------------------------------------------------------------
# The worker loop.
# ---------------------------------------------------------------------------


def test_worker_runs_a_handler_and_completes(migrated: Connection, db_dsn: str) -> None:
    seen: list[Job] = []
    job_id = enqueue(migrated, "k", {"n": 7})
    assert job_id is not None

    worker = Worker(db_dsn, {"k": seen.append}, WorkerConfig(name="w"))

    assert worker.run_once(migrated) is True
    assert [job.payload for job in seen] == [{"n": 7}]
    assert status_of(migrated, job_id) == "succeeded"


def test_worker_reports_nothing_to_do(migrated: Connection, db_dsn: str) -> None:
    assert Worker(db_dsn, {"k": lambda _job: None}).run_once(migrated) is False


def test_worker_turns_a_handler_exception_into_a_retry(migrated: Connection, db_dsn: str) -> None:
    """A failing handler is data about the world, not a reason to stop working."""

    def explode(_job: Job) -> None:
        raise ValueError("upstream 500")

    job_id = enqueue(migrated, "k", max_attempts=3)
    assert job_id is not None

    Worker(db_dsn, {"k": explode}, WorkerConfig(name="w")).run_once(migrated)

    row = row_of(migrated, job_id)
    assert row["status"] == "pending"
    assert row["last_error"] == "ValueError: upstream 500"


def test_worker_dead_letters_a_persistently_failing_job(migrated: Connection, db_dsn: str) -> None:
    def explode(_job: Job) -> None:
        raise RuntimeError("still broken")

    job_id = enqueue(migrated, "k", max_attempts=2)
    assert job_id is not None
    worker = Worker(db_dsn, {"k": explode}, WorkerConfig(name="w", base_backoff=0.001))

    worker.run_once(migrated)
    time.sleep(0.05)
    worker.run_once(migrated)

    assert status_of(migrated, job_id) == "dead"


def test_worker_drains_the_queue(migrated: Connection, db_dsn: str) -> None:
    for n in range(5):
        enqueue(migrated, "k", {"n": n})

    ran: list[UUID] = []
    worker = Worker(db_dsn, {"k": lambda job: ran.append(job.id)}, WorkerConfig(name="w"))

    assert worker.drain(migrated) == 5
    assert len(ran) == 5


def test_drain_honours_its_limit(migrated: Connection, db_dsn: str) -> None:
    for n in range(5):
        enqueue(migrated, "k", {"n": n})

    worker = Worker(db_dsn, {"k": lambda _job: None}, WorkerConfig(name="w"))

    assert worker.drain(migrated, limit=2) == 2
    assert queue_depth(migrated) == {"pending": 3, "succeeded": 2}


def test_worker_only_advertises_kinds_it_can_handle(db_dsn: str) -> None:
    worker = Worker(db_dsn, {"b": lambda _j: None, "a": lambda _j: None})
    assert worker.kinds == ("a", "b")


def test_worker_gets_a_default_name(db_dsn: str) -> None:
    assert ":" in Worker(db_dsn, {}).name


def test_run_forever_stops_when_asked(migrated: Connection, db_dsn: str) -> None:
    enqueue(migrated, "k", {"n": 1})
    done = threading.Event()
    halt = threading.Event()

    worker = Worker(
        db_dsn,
        {"k": lambda _job: done.set()},
        WorkerConfig(name="looping", poll_interval=0.05),
    )
    thread = threading.Thread(target=worker.run_forever, args=(halt,))
    thread.start()

    assert done.wait(timeout=15), "the looping worker never picked up the job"
    halt.set()
    thread.join(timeout=15)

    assert thread.is_alive() is False


def test_an_idle_loop_wakes_on_notify(migrated: Connection, db_dsn: str) -> None:
    """A job enqueued while the worker is idle wakes it through LISTEN, rather
    than waiting out the poll interval."""
    done = threading.Event()
    halt = threading.Event()
    worker = Worker(
        db_dsn,
        {"k": lambda _job: done.set()},
        # Long poll interval: if this test passes quickly, it was the
        # notification and not the poll that woke the worker.
        WorkerConfig(name="listening", poll_interval=30.0),
    )
    thread = threading.Thread(target=worker.run_forever, args=(halt,))
    thread.start()
    time.sleep(0.5)  # let it reach the LISTEN

    enqueue(migrated, "k", {"n": 1})
    woke = done.wait(timeout=10)

    halt.set()
    enqueue(migrated, "k", {"n": 2})  # nudge it out of the wait so it can exit
    thread.join(timeout=35)

    assert woke, "the worker slept through a notification"


def test_run_until_signalled_returns_on_sigterm(migrated: Connection, db_dsn: str) -> None:
    """Graceful shutdown, measured in-process. tests/test_jobs_crash.py proves
    the same thing for a real subprocess; this one proves the handlers are
    installed on the way in."""
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    worker = Worker(
        db_dsn, {"k": lambda _job: None}, WorkerConfig(name="signalled", poll_interval=0.05)
    )

    def signal_once_installed() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if signal.getsignal(signal.SIGTERM) is not original[signal.SIGTERM]:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.01)

    sender = threading.Thread(target=signal_once_installed, daemon=True)
    sender.start()
    try:
        worker.run_until_signalled()
    finally:
        sender.join(timeout=20)
        for sig, handler in original.items():
            signal.signal(sig, handler)


def test_run_forever_reclaims_before_working(migrated: Connection, db_dsn: str) -> None:
    """A restarted worker picks up what the crashed one dropped."""
    stranded = uuid4()
    migrated.execute(
        "INSERT INTO jobs (id, kind, status, attempts, max_attempts, locked_by, lease_expires_at) "
        "VALUES (%s, 'k', 'running', 1, 5, 'ghost', now() - interval '1 minute')",
        (stranded,),
    )
    done = threading.Event()
    halt = threading.Event()

    worker = Worker(
        db_dsn, {"k": lambda _job: done.set()}, WorkerConfig(name="fresh", poll_interval=0.05)
    )
    thread = threading.Thread(target=worker.run_forever, args=(halt,))
    thread.start()
    picked_up = done.wait(timeout=15)
    halt.set()
    thread.join(timeout=15)

    assert picked_up, "the stranded job was never reclaimed"
    assert status_of(migrated, stranded) == "succeeded"
