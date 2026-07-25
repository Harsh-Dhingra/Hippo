-- ============================================================
-- Hippo — ACLs in source terms (P1-SYNC-2)
--
-- The problem this solves: acl_grants.entity_id references entities, and only
-- the resolver creates entities. Sync therefore cannot write an acl_grants row
-- for content it has just fetched, because the entity does not exist yet. That
-- would couple ACL propagation to resolution and put the five-minute
-- revocation target in P1-SYNC-4 at the mercy of the resolver's schedule.
--
-- So sync writes here, in the only terms a connector knows, and a projection
-- fills acl_grants once entities exist. Two consequences worth stating:
--
--   * Revocation is immediate and resolver-independent. Deleting a source grant
--     removes the projected grant on the next projection, which the ACL
--     fast-lane runs on its own cadence.
--   * Until the resolver has run there is no entity, therefore no chunk,
--     therefore nothing to leak. An unprojected grant is not a hole.
--
-- The permission filter is untouched. acl_grants remains the single thing it
-- reads, and this table is upstream of it, never beside it.
-- ============================================================

-- ------------------------------------------------------------
-- Containment, recorded as source truth
-- ------------------------------------------------------------
-- Which container an object lives in is a fact the source stated, so it
-- belongs next to the payload rather than inside it: the payload stays
-- verbatim, and the SDK's ContentRecord.container has somewhere to land.
ALTER TABLE raw_records
    ADD COLUMN container_source_type text,
    ADD COLUMN container_source_id   text;

ALTER TABLE raw_records
    ADD CONSTRAINT raw_records_container_is_whole
    CHECK (num_nonnulls(container_source_type, container_source_id) <> 1);

CREATE INDEX raw_records_container_idx
    ON raw_records (connector_id, container_source_type, container_source_id)
    WHERE container_source_id IS NOT NULL;

-- ------------------------------------------------------------
-- Source-term grants
-- ------------------------------------------------------------
CREATE TABLE acl_source_grants (
    connector_id       uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    target_source_type text NOT NULL,
    target_source_id   text NOT NULL,
    principal_id       uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    access             text NOT NULL DEFAULT 'read',
    synced_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (connector_id, target_source_type, target_source_id, principal_id, access),
    CONSTRAINT acl_source_grants_access_known CHECK (access IN ('read'))
);
CREATE INDEX acl_source_grants_target_idx
    ON acl_source_grants (connector_id, target_source_type, target_source_id);
CREATE INDEX acl_source_grants_principal_idx ON acl_source_grants (principal_id);

-- ------------------------------------------------------------
-- Projection
-- ------------------------------------------------------------
-- Source grants name a container, because that is what source systems grant
-- on: sharing a Slack channel shares its messages, and a Jira project role
-- covers the project's issues and their comments. So access has to reach
-- everything inside the named object, at whatever depth the source nests
-- things, which is why the walk is recursive rather than one hop.
--
-- Containment is read from raw_records, not from edges, so projection depends
-- only on entities existing and not on the resolver's edge naming.
--
-- Rebuild rather than diff. One connector's grant set is small, the rebuild is
-- one statement, and a rebuild cannot drift the way an incremental diff can.
-- Getting this wrong is a permission bug, so it is deliberately the boring
-- implementation.
CREATE FUNCTION project_acl_grants(p_connector_id uuid) RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    inserted integer;
BEGIN
    DELETE FROM acl_grants WHERE source = p_connector_id::text;

    WITH RECURSIVE contained AS (
        -- The object the source system actually named.
        SELECT sg.principal_id, sg.access, r.id AS raw_record_id
        FROM acl_source_grants sg
        JOIN raw_records r
          ON r.connector_id = sg.connector_id
         AND r.source_type = sg.target_source_type
         AND r.source_id = sg.target_source_id
        WHERE sg.connector_id = p_connector_id

        UNION

        -- Anything whose container is something already granted. UNION, not
        -- UNION ALL: a containment cycle in bad data must not spin.
        SELECT c.principal_id, c.access, child.id
        FROM contained c
        JOIN raw_records parent ON parent.id = c.raw_record_id
        JOIN raw_records child
          ON child.connector_id = parent.connector_id
         AND child.container_source_type = parent.source_type
         AND child.container_source_id = parent.source_id
    )
    INSERT INTO acl_grants (entity_id, principal_id, access, source)
    SELECT DISTINCT es.entity_id, c.principal_id, c.access, p_connector_id::text
    FROM contained c
    JOIN entity_sources es ON es.raw_record_id = c.raw_record_id
    ON CONFLICT (entity_id, principal_id, access) DO NOTHING;

    GET DIAGNOSTICS inserted = ROW_COUNT;
    RETURN inserted;
END;
$$;

REVOKE ALL ON FUNCTION project_acl_grants(uuid) FROM PUBLIC;

-- ------------------------------------------------------------
-- Grants
-- ------------------------------------------------------------
-- Sync owns source grants and runs the projection; it already holds write on
-- acl_grants. The resolver reads them so it can project after creating
-- entities. The agent, as ever, gets nothing.
GRANT SELECT, INSERT, UPDATE, DELETE ON acl_source_grants TO hippo_sync;
GRANT SELECT ON acl_source_grants TO hippo_resolver;
GRANT EXECUTE ON FUNCTION project_acl_grants(uuid) TO hippo_sync;
