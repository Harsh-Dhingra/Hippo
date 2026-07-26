-- Reverts 016_timeline.sql.
--
-- occurred_at is derived from raw_records, which are immutable source truth, so
-- dropping it loses nothing that cannot be recomputed by re-running the
-- resolver. That is the whole reason source records are kept.

DROP FUNCTION IF EXISTS timeline(uuid, uuid, int, int);
DROP INDEX IF EXISTS entities_occurred_idx;
ALTER TABLE entities DROP COLUMN IF EXISTS occurred_at;
