-- ============================================================
-- Hippo — Jobs runtime (P1-CORE-4)
--
-- A table and SKIP LOCKED, per STACK.md. No Redis, no Celery, no broker.
-- The trade-off is stated in ARCHITECTURE §5: this caps throughput far below a
-- real broker and is orders of magnitude more than one org needs, and it keeps
-- the deploy story at two containers.
--
-- State machine:
--   pending -> running -> succeeded
--                      -> pending   (retry, run_at pushed out by backoff)
--                      -> dead      (attempts exhausted; the dead letter)
--
-- Crash safety is a lease, not a flag. A worker claims a job by setting an
-- expiry; if the worker dies the expiry passes and the job is reclaimed. A
-- claimed-flag scheme cannot recover from kill -9, because the dying process is
-- exactly the thing that would have had to clear the flag.
-- ============================================================

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL,               -- handler key, e.g. 'slack.sync.acls'
    payload          jsonb NOT NULL DEFAULT '{}',
    dedupe_key       text,                        -- at most one live job per key
    status           text NOT NULL DEFAULT 'pending',
    priority         int NOT NULL DEFAULT 100,    -- lower runs first
    attempts         int NOT NULL DEFAULT 0,
    max_attempts     int NOT NULL DEFAULT 5,
    run_at           timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz,
    locked_by        text,                        -- worker identity, for fencing and triage
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,

    CONSTRAINT jobs_status_known
        CHECK (status IN ('pending', 'running', 'succeeded', 'dead')),
    CONSTRAINT jobs_attempts_sane
        CHECK (attempts >= 0 AND max_attempts >= 1),
    -- A running job without an expiry is a job no crash can ever release.
    CONSTRAINT jobs_running_holds_a_lease
        CHECK (status <> 'running' OR (lease_expires_at IS NOT NULL AND locked_by IS NOT NULL)),
    CONSTRAINT jobs_finished_has_timestamp
        CHECK (status NOT IN ('succeeded', 'dead') OR completed_at IS NOT NULL)
);

-- The claim query's index: pending work, in run order.
CREATE INDEX jobs_claimable_idx ON jobs (priority, run_at, id) WHERE status = 'pending';
-- The reaper's index: leases that may have expired.
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at) WHERE status = 'running';
CREATE INDEX jobs_kind_status_idx ON jobs (kind, status);

-- Enqueue-once semantics for schedulers: re-enqueuing an ACL sync that is still
-- pending or running is a no-op rather than a second copy of the same work.
CREATE UNIQUE INDEX jobs_dedupe_key_live_idx ON jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');

CREATE TRIGGER jobs_set_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- LISTEN/NOTIFY as the wakeup, so an idle worker picks up new work immediately
-- instead of at the next poll. The poll loop remains the correctness mechanism;
-- notifications are only an optimisation, and a missed one costs latency, not a
-- lost job.
CREATE FUNCTION notify_job_ready() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM pg_notify('hippo_jobs', NEW.kind);
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION notify_job_ready() FROM PUBLIC;

CREATE TRIGGER jobs_notify_ready
    AFTER INSERT ON jobs
    FOR EACH ROW EXECUTE FUNCTION notify_job_ready();

-- ------------------------------------------------------------
-- Grants
-- ------------------------------------------------------------
-- Both worker processes run jobs. The agent gets nothing: it proposes actions
-- into the actions table, and it does not schedule work.
GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO hippo_sync, hippo_resolver;
