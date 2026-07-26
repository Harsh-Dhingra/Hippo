-- ============================================================
-- Hippo — Notes people write themselves (P2-MEM-1)
--
-- memory_notes has existed since 001 and nothing has ever written to it. This
-- is the fragment that makes it real, and the design question worth answering
-- first is what a note *is*.
--
-- A NOTE THAT DOES NOT CHANGE AN ANSWER IS A STICKY NOTE
--
-- The story ARCHITECTURE tells is "transparent, editable memory": you can see
-- what the system believes about you and your team, and correct it. That is
-- only true if a correction actually corrects something. A notes feature that
-- lived beside retrieval, in its own tab, would be a place to type into.
--
-- So a note is projected into the same path synced content takes: an entity, a
-- chunk, and ACL grants. It is then retrieved by visible_chunks() like
-- anything else, cited like anything else, and visible in the trace like
-- anything else. No second read path, which is the only way to add a source of
-- truth here without weakening rule 1.
--
-- The note row stays the source of truth and the projection is derived, which
-- is the same shape as raw_records to entities: editing a note re-projects,
-- deleting it removes the projection, and a botched projection is fixed by
-- re-running rather than by asking the author to retype.
--
-- WHY A SECURITY DEFINER PROJECTION
--
-- The projection writes entities, chunks and acl_grants. hippo_api holds none
-- of those and should not: it is the role that serves browsers. Rather than
-- widen it, the projection is one granted function that does exactly this and
-- nothing else — the same move project_acl_grants() makes for the sync role.
--
-- SCOPE IS THE PERMISSION, AND IT ALREADY EXISTS
--
-- A personal scope is visible to its owner alone and a team scope to the
-- group's members, both already enforced by _visible_scope_ids. So the note's
-- scope decides who can read it, with no new rule to get wrong. The grants the
-- projection writes mirror that, because the filter is a conjunction and needs
-- both halves.
-- ============================================================

ALTER TABLE memory_notes ADD COLUMN entity_id uuid REFERENCES entities(id);
ALTER TABLE memory_notes ADD COLUMN superseded_at timestamptz;

COMMENT ON COLUMN memory_notes.entity_id IS
    'The projection of this note into the retrieval path. Derived: the note is '
    'the source of truth and re-projecting is always safe.';

CREATE INDEX memory_notes_author_idx ON memory_notes (author, created_at DESC);
CREATE INDEX memory_notes_about_idx ON memory_notes (about_entity)
    WHERE about_entity IS NOT NULL;
CREATE INDEX memory_notes_live_idx ON memory_notes (scope_id, pinned DESC, updated_at DESC)
    WHERE superseded_at IS NULL;

-- ------------------------------------------------------------
-- Somewhere to put a personal note
-- ------------------------------------------------------------
-- Created on demand rather than for every principal at sync time: most people
-- never write a note, and a scope per principal would be a row per account in
-- a table the permission filter joins on every query.
CREATE FUNCTION ensure_personal_scope(p_principal uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    scope_id uuid;
BEGIN
    SELECT s.id INTO scope_id
    FROM memory_scopes s
    WHERE s.scope_type = 'personal' AND s.owner_principal = p_principal;

    IF scope_id IS NOT NULL THEN
        RETURN scope_id;
    END IF;

    INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name)
    SELECT 'personal', p.id, 'user', coalesce(p.email, p.source_id, 'Personal')
    FROM principals p
    WHERE p.id = p_principal AND p.kind = 'user'
    RETURNING id INTO scope_id;

    IF scope_id IS NULL THEN
        RAISE EXCEPTION 'no user principal %', p_principal;
    END IF;
    RETURN scope_id;
END;
$$;

-- ------------------------------------------------------------
-- The projection
-- ------------------------------------------------------------
CREATE FUNCTION project_note(p_note uuid, p_embedding vector(1024) DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    note        record;
    entity      uuid;
    grantees    uuid[];
BEGIN
    SELECT * INTO note FROM memory_notes WHERE id = p_note;
    IF note IS NULL THEN
        RAISE EXCEPTION 'no note %', p_note;
    END IF;

    -- Who the scope makes this visible to, mirroring _visible_scope_ids. The
    -- filter is a conjunction, so the grant has to agree with the scope or the
    -- note is written and nobody can read it.
    SELECT CASE s.scope_type
        WHEN 'personal' THEN ARRAY[s.owner_principal]
        WHEN 'team' THEN ARRAY[s.owner_principal]
        ELSE (SELECT coalesce(array_agg(p.id), '{}') FROM principals p WHERE p.kind = 'user')
    END INTO grantees
    FROM memory_scopes s WHERE s.id = note.scope_id;

    IF note.entity_id IS NULL THEN
        INSERT INTO entities (entity_type, title, attrs)
        VALUES ('note', left(note.content, 120), jsonb_build_object('source', 'note'))
        RETURNING id INTO entity;

        UPDATE memory_notes SET entity_id = entity WHERE id = p_note;
    ELSE
        entity := note.entity_id;
        UPDATE entities SET title = left(note.content, 120) WHERE id = entity;
    END IF;

    -- Content-addressed, so re-projecting an unchanged note keeps its
    -- embedding rather than paying to compute the same vector again.
    DELETE FROM chunks c
    WHERE c.entity_id = entity
      AND c.content_hash <> encode(sha256(convert_to(note.content, 'UTF8')), 'hex');

    INSERT INTO chunks (entity_id, scope_id, content, chunk_index, embedding)
    VALUES (entity, note.scope_id, note.content, 0, p_embedding)
    ON CONFLICT (entity_id, content_hash) DO UPDATE
        SET scope_id = EXCLUDED.scope_id,
            embedding = COALESCE(EXCLUDED.embedding, chunks.embedding);

    DELETE FROM acl_grants WHERE entity_id = entity AND source = 'note';
    INSERT INTO acl_grants (entity_id, principal_id, access, source)
    SELECT entity, grantee, 'read', 'note' FROM unnest(grantees) AS grantee
    WHERE grantee IS NOT NULL
    ON CONFLICT DO NOTHING;

    -- A superseded note stops being retrievable but keeps its row, because the
    -- audit question "what did this used to say" is the point of an editable
    -- memory.
    IF note.superseded_at IS NOT NULL THEN
        DELETE FROM chunks WHERE entity_id = entity;
    END IF;

    RETURN entity;
END;
$$;

CREATE FUNCTION unproject_note(p_note uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    entity uuid;
BEGIN
    SELECT entity_id INTO entity FROM memory_notes WHERE id = p_note;
    IF entity IS NULL THEN
        RETURN;
    END IF;
    DELETE FROM chunks WHERE entity_id = entity;
    DELETE FROM acl_grants WHERE entity_id = entity AND source = 'note';
    UPDATE memory_notes SET entity_id = NULL WHERE id = p_note;
    DELETE FROM entities WHERE id = entity;
END;
$$;

-- ------------------------------------------------------------
-- Reading notes as notes
-- ------------------------------------------------------------
-- Retrieval reaches a note through visible_chunks like any other content. This
-- is the other surface: the list you edit from, which needs the note's own
-- identity, its scope, and whether it is yours to change.
CREATE FUNCTION my_notes(p_principal uuid, p_limit int DEFAULT 100)
RETURNS TABLE (
    id            uuid,
    scope_id      uuid,
    scope_type    text,
    scope_name    text,
    author        uuid,
    is_mine       boolean,
    about_entity  uuid,
    content       text,
    pinned        boolean,
    superseded_at timestamptz,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT n.id, n.scope_id, s.scope_type, s.name, n.author,
           n.author IN (SELECT sp.principal_id FROM _same_person(p_principal) sp),
           n.about_entity, n.content, n.pinned, n.superseded_at, n.created_at, n.updated_at
    FROM memory_notes n
    JOIN memory_scopes s ON s.id = n.scope_id
    WHERE n.scope_id IN (SELECT vs.scope_id FROM _visible_scope_ids(p_principal) vs)
    ORDER BY n.pinned DESC, n.updated_at DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500);
$$;

-- Which scopes this person may write into. A note in a scope you cannot see is
-- a note you cannot read back, so the write side has to agree with the read.
CREATE FUNCTION my_scopes(p_principal uuid)
RETURNS TABLE (id uuid, scope_type text, name text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT s.id, s.scope_type, s.name
    FROM memory_scopes s
    WHERE s.id IN (SELECT vs.scope_id FROM _visible_scope_ids(p_principal) vs)
    ORDER BY CASE s.scope_type WHEN 'personal' THEN 0 WHEN 'team' THEN 1 ELSE 2 END, s.name;
$$;

REVOKE ALL ON FUNCTION ensure_personal_scope(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION project_note(uuid, vector) FROM PUBLIC;
REVOKE ALL ON FUNCTION unproject_note(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION my_notes(uuid, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION my_scopes(uuid) FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON memory_notes TO hippo_api;
GRANT EXECUTE ON FUNCTION ensure_personal_scope(uuid) TO hippo_api;
GRANT EXECUTE ON FUNCTION project_note(uuid, vector) TO hippo_api;
GRANT EXECUTE ON FUNCTION unproject_note(uuid) TO hippo_api;
GRANT EXECUTE ON FUNCTION my_notes(uuid, int) TO hippo_api;
GRANT EXECUTE ON FUNCTION my_scopes(uuid) TO hippo_api;
