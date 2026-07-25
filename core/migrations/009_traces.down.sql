-- Reverts 009_traces.sql.
--
-- Dropping the tables drops the traces. That is data loss, and it is the
-- correct behaviour for a down migration of a table that only this migration
-- created: keeping orphaned rows around would leave a copy of every question
-- anyone asked, readable by whatever grants exist next.

DROP FUNCTION IF EXISTS my_trace(uuid, uuid);
DROP FUNCTION IF EXISTS my_traces(uuid, int);
DROP TABLE IF EXISTS trace_retrievals;
DROP TABLE IF EXISTS query_traces;
