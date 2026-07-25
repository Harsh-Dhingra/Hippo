-- Reverts 011_writeback.sql.
--
-- Any action caught mid-flight becomes 'failed'. The narrower vocabulary has no
-- way to say "we do not know whether this landed", and 'failed' is the reading
-- that sends someone to look rather than the one that says all is well.

UPDATE actions SET status = 'failed', error = coalesce(error, 'in flight at downgrade')
    WHERE status IN ('executing', 'rolling_back');

DROP INDEX IF EXISTS actions_in_flight_idx;
DROP INDEX IF EXISTS actions_awaiting_execution_idx;

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_rollback_names_its_person;

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_executed_has_timestamp;
ALTER TABLE actions ADD CONSTRAINT actions_executed_has_timestamp
    CHECK (status NOT IN ('executed', 'rolled_back') OR executed_at IS NOT NULL);

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_inverse_captured_before_execution;
ALTER TABLE actions ADD CONSTRAINT actions_inverse_captured_before_execution
    CHECK (status NOT IN ('executed', 'rolled_back') OR inverse_payload IS NOT NULL);

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_status_known;
ALTER TABLE actions ADD CONSTRAINT actions_status_known
    CHECK (status IN ('pending', 'approved', 'declined', 'executed', 'rolled_back', 'failed'));

ALTER TABLE actions DROP COLUMN IF EXISTS rolled_back_by;
ALTER TABLE actions DROP COLUMN IF EXISTS rolled_back_at;
ALTER TABLE actions DROP COLUMN IF EXISTS execution_started_at;
ALTER TABLE actions DROP COLUMN IF EXISTS receipt;
