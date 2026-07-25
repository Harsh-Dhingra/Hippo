-- ============================================================
-- Hippo — Schema v0
-- Postgres 16 + pgvector. Six core concepts, nothing more.
-- Design rules:
--   1. Permissions are first-class nodes, evaluated at query time.
--   2. Every write-back stores its inverse (rollback is data, not hope).
--   3. Memory scopes (org/team/personal) are structural, not tags.
--   4. Source-system truth is preserved; canonical IDs layer on top.
--
-- Constraints here are load-bearing, not decoration. Where a rule in CLAUDE.md
-- can be enforced by the database, it is: an executed action cannot exist
-- without its inverse, and a model-inferred edge cannot claim full confidence.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- 1. CONNECTORS & SYNC STATE
-- ------------------------------------------------------------
CREATE TABLE connectors (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,              -- 'slack' | 'jira' (v0: only these two)
    display_name    text NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}',-- workspace ids, base urls; NEVER secrets
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- No CHECK on kind: a new connector must not require a schema migration.
-- The closed set lives in the connector registry, validated by pydantic.

CREATE TABLE sync_state (
    connector_id    uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
    stream          text NOT NULL,              -- 'messages', 'issues', 'users', 'acls'
    cursor          jsonb NOT NULL DEFAULT '{}',-- per-stream cursor (ts, page token, etc.)
    schema_version  text,                       -- detect upstream schema drift
    last_synced_at  timestamptz,
    last_error      text,
    PRIMARY KEY (connector_id, stream)
);

-- ------------------------------------------------------------
-- 2. ENTITIES (typed nodes)
-- ------------------------------------------------------------
-- Raw records keep source truth. Canonical entities layer identity
-- resolution on top without destroying provenance.

CREATE TABLE raw_records (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id    uuid NOT NULL REFERENCES connectors(id),
    source_type     text NOT NULL,              -- 'slack.message', 'jira.issue', ...
    source_id       text NOT NULL,              -- id in the source system
    payload         jsonb NOT NULL,             -- verbatim source object
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (connector_id, source_type, source_id)
);

CREATE TABLE entities (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type     text NOT NULL,              -- 'person','account','ticket','thread','doc','message'
    canonical_key   text,                       -- e.g. lowercased email for person merge
    title           text,
    summary         text,                       -- model-written, regenerated on change
    attrs           jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON entities (entity_type);
CREATE UNIQUE INDEX ON entities (entity_type, canonical_key)
    WHERE canonical_key IS NOT NULL;

-- Provenance: which raw records back this entity (n:1 after resolution)
CREATE TABLE entity_sources (
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    raw_record_id   uuid NOT NULL REFERENCES raw_records(id) ON DELETE CASCADE,
    PRIMARY KEY (entity_id, raw_record_id)
);

-- ------------------------------------------------------------
-- 3. EDGES (typed, provenanced relations)
-- ------------------------------------------------------------
CREATE TABLE edges (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_id          uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id          uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    edge_type       text NOT NULL,              -- 'mentions','blocks','belongs_to','authored','resolved_by'
    confidence      real NOT NULL DEFAULT 1.0,  -- 1.0 = from source structure; <1.0 = inferred by model
    provenance      text NOT NULL,              -- 'source' | 'resolver' | 'model'
    attrs           jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- Provenance is part of the identity of an edge. Without it, a model-inferred
    -- edge would silently overwrite the source edge it duplicates, and the
    -- deterministic fact would be lost with no trace.
    UNIQUE (src_id, dst_id, edge_type, provenance),

    CONSTRAINT edges_provenance_known
        CHECK (provenance IN ('source', 'resolver', 'model')),
    CONSTRAINT edges_confidence_in_range
        CHECK (confidence > 0.0 AND confidence <= 1.0),
    -- CLAUDE.md rule 5: inferred facts are never presented as source facts.
    CONSTRAINT edges_model_is_never_certain
        CHECK (provenance <> 'model' OR confidence < 1.0)
);
CREATE INDEX ON edges (src_id, edge_type);
CREATE INDEX ON edges (dst_id, edge_type);

-- ------------------------------------------------------------
-- 4. PERMISSIONS (first-class, synced from sources)
-- ------------------------------------------------------------
-- Principals mirror source-system users/groups; mapped to platform users.
CREATE TABLE principals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL,              -- 'user' | 'group'
    connector_id    uuid REFERENCES connectors(id),
    source_id       text,                       -- id in source system (slack user id, jira account id)
    email           text,                       -- join key across systems

    CONSTRAINT principals_kind_known CHECK (kind IN ('user', 'group')),
    -- Lets memory_scopes reference (id, kind) and so require that a team scope
    -- is owned by a group and a personal scope by a user.
    CONSTRAINT principals_id_kind_key UNIQUE (id, kind)
);

-- A NULL never equals a NULL, so a plain UNIQUE(connector_id, source_id) would
-- let unlimited platform-native principals collide. Split into two partial
-- indexes that each cover a case the other does not.
CREATE UNIQUE INDEX principals_connector_source_key
    ON principals (connector_id, source_id)
    WHERE connector_id IS NOT NULL AND source_id IS NOT NULL;
CREATE UNIQUE INDEX principals_platform_email_key
    ON principals (lower(email))
    WHERE connector_id IS NULL AND email IS NOT NULL;

-- Deliberately NOT unique: the same human in Slack and in Jira is two
-- principals with one email. Merging them is P1-RES-2's job, and it happens on
-- entities.canonical_key, not here.
CREATE INDEX principals_email_idx ON principals (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE principal_memberships (             -- group expansion, synced
    group_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    member_id       uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, member_id),
    CONSTRAINT principal_memberships_no_self_loop CHECK (group_id <> member_id)
);
CREATE INDEX ON principal_memberships (member_id);

-- ACL grants: who can see which entity. Synced from source ACLs.
-- Deny-by-default: no grant row => not visible.
CREATE TABLE acl_grants (
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    principal_id    uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    access          text NOT NULL DEFAULT 'read',   -- v0: 'read' only
    source          text NOT NULL,                   -- which connector asserted this
    PRIMARY KEY (entity_id, principal_id, access),
    CONSTRAINT acl_grants_access_known CHECK (access IN ('read'))
);
CREATE INDEX ON acl_grants (principal_id);

-- ------------------------------------------------------------
-- 5. MEMORY SCOPES + CHUNKS (retrieval layer)
-- ------------------------------------------------------------
-- Scope ownership is structural, so the scope half of the permission filter has
-- exactly one meaning: an org scope is visible to everyone, a team scope to the
-- members of the owning group, a personal scope to its owner alone. The group
-- expansion that answers the ACL half answers this half too.
CREATE TABLE memory_scopes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type      text NOT NULL,              -- 'org' | 'team' | 'personal'
    owner_principal uuid,
    owner_kind      text,                       -- mirrors principals.kind; see FK below
    name            text NOT NULL,

    FOREIGN KEY (owner_principal, owner_kind) REFERENCES principals (id, kind),
    CONSTRAINT memory_scopes_type_known
        CHECK (scope_type IN ('org', 'team', 'personal')),
    CONSTRAINT memory_scopes_owner_matches_type CHECK (
        (scope_type = 'org'      AND owner_principal IS NULL     AND owner_kind IS NULL)
     OR (scope_type = 'team'     AND owner_principal IS NOT NULL AND owner_kind = 'group')
     OR (scope_type = 'personal' AND owner_principal IS NOT NULL AND owner_kind = 'user')
    )
);
CREATE INDEX ON memory_scopes (owner_principal);

-- Every chunk needs a scope, so the org scope exists from migration time. The
-- id is fixed so seeds, fixtures and the resolver can reference it directly.
INSERT INTO memory_scopes (id, scope_type, owner_principal, owner_kind, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'org', NULL, NULL, 'Organization');

-- Chunks are the embeddable units. Every chunk points at its entity,
-- inherits its ACL, and lives in exactly one scope.
CREATE TABLE chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    scope_id        uuid NOT NULL REFERENCES memory_scopes(id),
    content         text NOT NULL,
    embedding       vector(1024),               -- pick model later; dim is a config decision
    token_count     int,
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- Keyword half of hybrid retrieval (P1-AGT-2). Generated, not trigger-fed,
    -- so it cannot drift from content.
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks USING gin (content_tsv);
CREATE INDEX ON chunks (entity_id);
CREATE INDEX ON chunks (scope_id);

-- Curation: user-visible, editable memory notes (the "personal layer").
CREATE TABLE memory_notes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id        uuid NOT NULL REFERENCES memory_scopes(id),
    author          uuid NOT NULL REFERENCES principals(id),
    about_entity    uuid REFERENCES entities(id),
    content         text NOT NULL,
    pinned          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_notes (scope_id);

-- ------------------------------------------------------------
-- 6. ACTIONS (write-back with stored inverse)
-- ------------------------------------------------------------
CREATE TABLE actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_by    uuid NOT NULL REFERENCES principals(id),
    connector_id    uuid NOT NULL REFERENCES connectors(id),
    action_type     text NOT NULL,              -- 'jira.comment', 'jira.transition', 'slack.post'
    target_entity   uuid REFERENCES entities(id),
    payload         jsonb NOT NULL,             -- what we intend to write
    risk_class      text NOT NULL,              -- 'routine' | 'consequential'
    status          text NOT NULL DEFAULT 'pending',
                                                -- pending -> approved -> executed -> (rolled_back | failed)
    approved_by     uuid REFERENCES principals(id),
    inverse_payload jsonb,                      -- captured pre-execution state; how we roll back
    executed_at     timestamptz,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT actions_status_known
        CHECK (status IN ('pending', 'approved', 'executed', 'rolled_back', 'failed')),
    CONSTRAINT actions_risk_class_known
        CHECK (risk_class IN ('routine', 'consequential')),
    -- CLAUDE.md rule 3: no inverse capture means the action fails, never
    -- "executes without rollback". The database refuses to record it otherwise.
    CONSTRAINT actions_inverse_captured_before_execution
        CHECK (status NOT IN ('executed', 'rolled_back') OR inverse_payload IS NOT NULL),
    CONSTRAINT actions_executed_has_timestamp
        CHECK (status NOT IN ('executed', 'rolled_back') OR executed_at IS NOT NULL),
    -- CLAUDE.md rule 2: nothing executes without a human on the record.
    CONSTRAINT actions_execution_requires_approval
        CHECK (status IN ('pending', 'failed') OR approved_by IS NOT NULL)
);
CREATE INDEX ON actions (status);
CREATE INDEX ON actions (connector_id, status);

-- ------------------------------------------------------------
-- HOUSEKEEPING
-- ------------------------------------------------------------
CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER entities_set_updated_at
    BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER memory_notes_set_updated_at
    BEFORE UPDATE ON memory_notes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------------
-- QUERY-TIME PERMISSION FILTER (the whole ballgame)
-- ------------------------------------------------------------
-- Every retrieval joins through this. No embedding search result
-- reaches the model unless the asking principal can see the entity.
--
-- SELECT c.content, c.entity_id
-- FROM chunks c
-- JOIN acl_grants g ON g.entity_id = c.entity_id
-- WHERE g.principal_id IN (
--     SELECT :asking_principal
--     UNION
--     SELECT group_id FROM principal_memberships WHERE member_id = :asking_principal
-- )
-- AND (scope check: org scope, or team scope user belongs to, or own personal scope)
-- ORDER BY c.embedding <=> :query_embedding
-- LIMIT 20;
--
-- v0 rule: this filter lives in ONE function. Nothing bypasses it. Ever.
-- The function itself arrives in P1-CORE-3, and the agent role's EXECUTE grant
-- with it. Until then the agent role can read no content at all, which is the
-- correct default.
