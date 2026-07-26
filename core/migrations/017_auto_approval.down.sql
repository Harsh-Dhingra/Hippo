-- Reverts 017_auto_approval.sql.
--
-- Any action approved by policy rather than by a person becomes pending again,
-- because the narrower schema has no way to record why it was approved and
-- leaving it approved would assert that somebody looked at it. Sending it back
-- for a human is the safe direction.

UPDATE actions SET status = 'pending'
    WHERE approved_by_policy IS NOT NULL AND status = 'approved';

REVOKE UPDATE ON actions FROM hippo_sync;
GRANT UPDATE ON actions TO hippo_sync;

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_one_kind_of_approval;
ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_execution_requires_approval;
ALTER TABLE actions ADD CONSTRAINT actions_execution_requires_approval
    CHECK (status IN ('pending', 'declined', 'failed') OR approved_by IS NOT NULL);

DROP INDEX IF EXISTS actions_auto_approved_idx;
ALTER TABLE actions DROP COLUMN IF EXISTS approved_by_policy;
