-- Reverts 005_acl_source_grants.sql.

DROP FUNCTION IF EXISTS project_acl_grants(uuid);
DROP TABLE IF EXISTS acl_source_grants;

DROP INDEX IF EXISTS raw_records_container_idx;
ALTER TABLE raw_records DROP CONSTRAINT IF EXISTS raw_records_container_is_whole;
ALTER TABLE raw_records
    DROP COLUMN IF EXISTS container_source_type,
    DROP COLUMN IF EXISTS container_source_id;
