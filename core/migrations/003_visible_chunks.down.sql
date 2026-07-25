-- Reverts 003_visible_chunks.sql.

GRANT EXECUTE ON FUNCTION set_updated_at() TO PUBLIC;

DROP FUNCTION IF EXISTS visible_chunks(uuid, text, vector, int, int);
DROP FUNCTION IF EXISTS _visible_scope_ids(uuid);
DROP FUNCTION IF EXISTS _visible_entity_ids(uuid);
DROP FUNCTION IF EXISTS _expanded_principals(uuid);

ALTER TABLE principal_memberships
    DROP CONSTRAINT IF EXISTS principal_memberships_group_is_a_group;
ALTER TABLE principal_memberships
    DROP CONSTRAINT IF EXISTS principal_memberships_group_kind_fixed;
ALTER TABLE principal_memberships
    DROP COLUMN IF EXISTS group_kind;
