-- Reverts 018_audit.sql.
--
-- Dropping the table destroys the audit log. That is data loss and it is the
-- correct behaviour for a table only this migration created: leaving the rows
-- behind would leave a record of every decision anyone made, governed by
-- whatever grants exist next.

DROP FUNCTION IF EXISTS my_action_events(uuid, text, timestamptz, int);
DROP TRIGGER IF EXISTS actions_record_event ON actions;
DROP FUNCTION IF EXISTS record_action_event();
DROP TABLE IF EXISTS action_events;
