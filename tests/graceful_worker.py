"""A worker that shuts down on a signal.

Run as a subprocess by the graceful-shutdown test:

    python -m tests.graceful_worker <dsn>

The counterpart to tests.crash_worker: this one is asked to stop politely and
must exit zero, where that one is killed and must leave its lease to expire.
"""

import sys

from core.db import connect
from core.jobs import Job, Worker, WorkerConfig

KIND = "graceful.touch"


def main(argv: list[str]) -> int:
    dsn = argv[0]

    def touch(job: Job) -> None:
        with connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO crash_marks (job_id, phase, worker) "
                "VALUES (%s, 'started', 'graceful')",
                (job.id,),
            )

    worker = Worker(
        dsn,
        {KIND: touch},
        WorkerConfig(name="graceful-worker", poll_interval=0.05),
    )
    worker.run_until_signalled()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
