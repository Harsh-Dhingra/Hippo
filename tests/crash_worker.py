"""A worker that can be killed mid-job.

Run as a subprocess by the crash test:

    python -m tests.crash_worker <dsn> <lease_seconds>

The handler records that it started and then blocks forever, so the parent can
SIGKILL it at a known point: after the job is claimed and before it completes.
Nothing here cleans up on the way out, which is the point.
"""

import sys
import time

from core.db import connect
from core.jobs import Job, Worker, WorkerConfig

KIND = "crash.sleep"


def main(argv: list[str]) -> int:
    dsn, lease_seconds = argv[0], float(argv[1])

    def block_forever(job: Job) -> None:
        with connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO crash_marks (job_id, phase, worker) VALUES (%s, 'started', 'crash')",
                (job.id,),
            )
        time.sleep(3600)

    worker = Worker(
        dsn,
        {KIND: block_forever},
        WorkerConfig(name="crash-worker", lease_seconds=lease_seconds, poll_interval=0.05),
    )
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
