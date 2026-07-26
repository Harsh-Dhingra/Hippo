-- Reverts 015_memory_notes.sql.
--
-- The projections go first, so no chunk is left pointing at a note nobody can
-- edit any more. The note rows themselves survive: they are what a person
-- wrote, and a downgrade should not be a way to lose it.

DO $$
DECLARE
    note uuid;
BEGIN
    FOR note IN SELECT id FROM memory_notes WHERE entity_id IS NOT NULL LOOP
        PERFORM unproject_note(note);
    END LOOP;
END
$$;

DROP FUNCTION IF EXISTS my_scopes(uuid);
DROP FUNCTION IF EXISTS my_notes(uuid, int);
DROP FUNCTION IF EXISTS unproject_note(uuid);
DROP FUNCTION IF EXISTS project_note(uuid, vector);
DROP FUNCTION IF EXISTS ensure_personal_scope(uuid);

REVOKE ALL ON memory_notes FROM hippo_api;

DROP INDEX IF EXISTS memory_notes_live_idx;
DROP INDEX IF EXISTS memory_notes_about_idx;
DROP INDEX IF EXISTS memory_notes_author_idx;
ALTER TABLE memory_notes DROP COLUMN IF EXISTS superseded_at;
ALTER TABLE memory_notes DROP COLUMN IF EXISTS entity_id;
