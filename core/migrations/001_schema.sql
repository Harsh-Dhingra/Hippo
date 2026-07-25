-- ============================================================
-- Hippo — Schema v0
-- Postgres 16 + pgvector. Six core concepts, nothing more.
-- Design rules:
--   1. Permissions are first-class nodes, evaluated at query time.
--   2. Every write-back stores its inverse (rollback is data, not hope).
--   3. Memory scopes (org/team/personal) are structural, not tags.
--   4. Source-system truth is preserved; canonical IDs layer on top.
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
    UNIQUE (src_id, dst_id, edge_type)
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
    UNIQUE (connector_id, source_id)
);

CREATE TABLE principal_memberships (             -- group expansion, synced
    group_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    member_id       uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, member_id)
);

-- ACL grants: who can see which entity. Synced from source ACLs.
-- Deny-by-default: no grant row => not visible.
CREATE TABLE acl_grants (
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    principal_id    uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    access          text NOT NULL DEFAULT 'read',   -- v0: 'read' only
    source          text NOT NULL,                   -- which connector asserted this
    PRIMARY KEY (entity_id, principal_id, access)
);
CREATE INDEX ON acl_grants (principal_id);

-- ------------------------------------------------------------
-- 5. MEMORY SCOPES + CHUNKS (retrieval layer)
-- ------------------------------------------------------------
CREATE TABLE memory_scopes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type      text NOT NULL,              -- 'org' | 'team' | 'personal'
    owner_principal uuid REFERENCES principals(id),  -- required for 'personal'
    name            text NOT NULL
);

-- Chunks are the embeddable units. Every chunk points at its entity,
-- inherits its ACL, and lives in exactly one scope.
CREATE TABLE chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    scope_id        uuid NOT NULL REFERENCES memory_scopes(id),
    content         text NOT NULL,
    embedding       vector(1024),               -- pick model later; dim is a config decision
    token_count     int,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks (entity_id);

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
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON actions (status);

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
