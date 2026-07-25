-- ============================================================
-- Hippo — Executing approved actions (P1-SYNC-5)
--
-- ARCHITECTURE §4 point 3: on approval, the sync worker captures the current
-- state as inverse_payload, executes, and marks executed; rollback executes the
-- inverse. 001 built the table for that. This adds the three things doing it
-- for real turned out to need.
--
-- TWO TRANSIENT STATUSES, BECAUSE CLAIMING IS A STATUS CHANGE
--
-- `UPDATE ... WHERE status = 'approved' RETURNING ...` is what makes two
-- workers safe: the second matches no row and does nothing. Without a status to
-- move into, claiming would have to be a read followed by a write, and the
-- window between them is where an approved action gets performed twice. For a
-- system whose selling point is that it does not act without permission,
-- "the comment appeared twice" is a bad way to fail.
--
-- 'executing' and 'rolling_back' are both reachable only from a state that
-- already has an approver, so the approval constraint covers them without
-- being widened.
--
-- A CRASHED EXECUTOR IS NOT A RETRYABLE JOB
--
-- execution_started_at exists so a row stuck in 'executing' can be found. What
-- happens to it is deliberately not an automatic retry: the write may have
-- half-landed, and repeating it blindly is how one approval becomes two
-- comments. The reaper moves it to 'failed' with a message telling a person to
-- look, which is the honest outcome when the system genuinely does not know
-- whether the write happened.
--
-- THE RECEIPT
--
-- What the source said back. It is what makes an executed action checkable
-- against Jira afterwards, and for a create it carries the id the rollback
-- needs — the inverse of creating something is deleting it, and the id does not
-- exist until the create returns.
-- ============================================================

ALTER TABLE actions ADD COLUMN receipt jsonb;
ALTER TABLE actions ADD COLUMN execution_started_at timestamptz;
ALTER TABLE actions ADD COLUMN rolled_back_at timestamptz;
ALTER TABLE actions ADD COLUMN rolled_back_by uuid REFERENCES principals(id);

COMMENT ON COLUMN actions.receipt IS
    'What the source system returned. For a create, carries the id the rollback '
    'deletes.';

ALTER TABLE actions DROP CONSTRAINT actions_status_known;
ALTER TABLE actions ADD CONSTRAINT actions_status_known
    CHECK (status IN (
        'pending', 'approved', 'declined',
        'executing', 'executed',
        'rolling_back', 'rolled_back',
        'failed'
    ));

-- Rule 3, restated for the new statuses: rolling_back is only reachable from
-- executed, which already required an inverse, but saying so here means a
-- future path into it cannot skip the requirement.
ALTER TABLE actions DROP CONSTRAINT actions_inverse_captured_before_execution;
ALTER TABLE actions ADD CONSTRAINT actions_inverse_captured_before_execution
    CHECK (
        status NOT IN ('executed', 'rolling_back', 'rolled_back')
        OR inverse_payload IS NOT NULL
    );

ALTER TABLE actions DROP CONSTRAINT actions_executed_has_timestamp;
ALTER TABLE actions ADD CONSTRAINT actions_executed_has_timestamp
    CHECK (
        status NOT IN ('executed', 'rolling_back', 'rolled_back')
        OR executed_at IS NOT NULL
    );

-- A rollback is a decision too, and the audit log names who made it.
ALTER TABLE actions ADD CONSTRAINT actions_rollback_names_its_person
    CHECK (status <> 'rolled_back' OR (rolled_back_by IS NOT NULL AND rolled_back_at IS NOT NULL));

CREATE INDEX actions_awaiting_execution_idx ON actions (created_at)
    WHERE status = 'approved';
CREATE INDEX actions_in_flight_idx ON actions (execution_started_at)
    WHERE status IN ('executing', 'rolling_back');

-- ------------------------------------------------------------
-- Grants
-- ------------------------------------------------------------
-- The API records a rollback request the same way it records an approval: by
-- moving the status. The write itself still belongs to hippo_sync, which is the
-- only role holding a Jira credential.
--
-- hippo_sync already holds SELECT, UPDATE on actions from 002; nothing widens
-- here. This migration adds columns to a table whose grants are already
-- decided, which is exactly the case 002's "no ALTER DEFAULT PRIVILEGES" note
-- was written for.
