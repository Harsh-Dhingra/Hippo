-- ============================================================
-- Hippo — Roles and grants (ARCHITECTURE §9)
--
-- Defense in depth is the schema. Three roles, each able to do exactly its job
-- and nothing else:
--
--   hippo_sync      raw records, ACLs, principals, sync state; executes
--                   approved actions. Holds the source-system credentials.
--   hippo_resolver  reads raw records, writes the graph. Never calls a source.
--   hippo_agent     INSERT on actions. No SELECT on anything. Content access
--                   arrives in P1-CORE-3 as EXECUTE on visible_chunks(), and
--                   that function is the only read path it will ever have.
--
-- These are NOLOGIN group roles on purpose: the operator creates login users
-- and grants membership, so no password is ever written into a migration.
--
--   CREATE USER hippo_agent_svc LOGIN PASSWORD '...';
--   GRANT hippo_agent TO hippo_agent_svc;
--
-- No ALTER DEFAULT PRIVILEGES anywhere in this file. A future migration adding
-- a table must decide that table's grants explicitly; nothing is readable by
-- accident. The leak test fails on any table the grant matrix does not mention.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_sync') THEN
        CREATE ROLE hippo_sync NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_resolver') THEN
        CREATE ROLE hippo_resolver NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_agent') THEN
        CREATE ROLE hippo_agent NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

-- Reachability only. Being able to see the schema is not being able to read it.
GRANT USAGE ON SCHEMA public TO hippo_sync, hippo_resolver, hippo_agent;

-- No service role creates objects. Migrations run as the owner.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM hippo_sync, hippo_resolver, hippo_agent;

-- ------------------------------------------------------------
-- hippo_sync — owns everything that comes from a source system
-- ------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    connectors,
    sync_state,
    raw_records,
    principals,
    principal_memberships,
    acl_grants
TO hippo_sync;

-- Write-back: read the queue, record the inverse and the outcome. It may not
-- create actions, because proposing is the agent's job and approving is a
-- human's.
GRANT SELECT, UPDATE ON actions TO hippo_sync;

-- Enough to resolve an action's target back to a source object. Read only:
-- the graph belongs to the resolver.
GRANT SELECT ON entities, entity_sources TO hippo_sync;

-- ------------------------------------------------------------
-- hippo_resolver — reads source truth, writes the graph
-- ------------------------------------------------------------
GRANT SELECT ON raw_records, connectors, principals, principal_memberships TO hippo_resolver;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    entities,
    entity_sources,
    edges,
    chunks
TO hippo_resolver;

-- Chunks are placed into scopes; scopes are not the resolver's to invent.
GRANT SELECT ON memory_scopes TO hippo_resolver;

-- Deliberately absent: acl_grants. Permissions come from the source system via
-- sync. A resolver that could write ACLs would be a resolver that could grant
-- itself access.

-- ------------------------------------------------------------
-- hippo_agent — proposes, and can read nothing
-- ------------------------------------------------------------
-- INSERT only, with no SELECT. Note for whoever writes the proposal code:
-- `INSERT ... RETURNING id` needs SELECT on the returned column and will fail
-- here. Generate the uuid client-side and insert it. The missing SELECT is the
-- point, not an oversight.
GRANT INSERT ON actions TO hippo_agent;
