-- Reverts 010_users_and_sessions.sql.
--
-- The role is left in place, for the same reason 002 is irreversible: roles are
-- cluster-wide and dropping one that another database still uses is a worse
-- outcome than leaving a role with no grants. Its grants go with the tables.

REVOKE ALL ON FUNCTION my_trace(uuid, uuid) FROM hippo_api;
REVOKE ALL ON FUNCTION my_traces(uuid, int) FROM hippo_api;
REVOKE ALL ON entities, connectors, principals, actions FROM hippo_api;

DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;

-- Restore 001's action statuses. Any row that was declined becomes failed,
-- because the narrower vocabulary has no way to say what happened to it.
UPDATE actions SET status = 'failed', error = coalesce(error, 'declined')
    WHERE status = 'declined';

ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_declined_names_the_decliner;
ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_status_known;
ALTER TABLE actions ADD CONSTRAINT actions_status_known
    CHECK (status IN ('pending', 'approved', 'executed', 'rolled_back', 'failed'));
ALTER TABLE actions DROP CONSTRAINT IF EXISTS actions_execution_requires_approval;
ALTER TABLE actions ADD CONSTRAINT actions_execution_requires_approval
    CHECK (status IN ('pending', 'failed') OR approved_by IS NOT NULL);
ALTER TABLE actions DROP COLUMN IF EXISTS declined_by;
