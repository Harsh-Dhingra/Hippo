-- ============================================================
-- Hippo — The permission filter (P1-CORE-3)
--
-- This is the load-bearing code of the whole project. Everything else can be
-- rewritten; this is the thing that must never be wrong.
--
-- One granted function, visible_chunks(). The agent role holds EXECUTE on it
-- and SELECT on nothing, so there is no second read path to construct.
--
-- Signature note: PROJECT.md sketches visible_chunks(principal, embedding, k).
-- It takes query text as well, because P1-AGT-2 fuses BM25-class keyword search
-- with vector search, and rule 1 forbids keyword search reaching chunks by any
-- other route. Ranking is not a security concern, but it has to happen inside
-- the filter, so the filter is where it happens.
--
-- Visibility is a conjunction of two independent checks:
--   ACL    an acl_grants row for the chunk's entity, held by the asking
--          principal or by a group that transitively contains it.
--   SCOPE  org scopes are visible to everyone; a team scope to members of the
--          owning group; a personal scope to its owner alone.
-- Both checks consume the same principal closure, so there is one expansion
-- to get right rather than two.
--
-- Deny by default throughout: no grant row means invisible, and every join is
-- an intersection with something already proven visible.
-- ============================================================

-- ------------------------------------------------------------
-- Membership integrity
-- ------------------------------------------------------------
-- The scope check leans on a property the schema did not yet enforce: that only
-- a group can appear as principal_memberships.group_id. Without it, a *user*
-- principal could be given members, and then that user's personal scope would
-- expand to them. The redundant column exists solely so a composite foreign key
-- can require principals.kind = 'group'; it has one legal value and writers can
-- ignore it.
ALTER TABLE principal_memberships
    ADD COLUMN group_kind text NOT NULL DEFAULT 'group';
ALTER TABLE principal_memberships
    ADD CONSTRAINT principal_memberships_group_kind_fixed
    CHECK (group_kind = 'group');
ALTER TABLE principal_memberships
    ADD CONSTRAINT principal_memberships_group_is_a_group
    FOREIGN KEY (group_id, group_kind) REFERENCES principals (id, kind);

-- ------------------------------------------------------------
-- The predicate, in one place
-- ------------------------------------------------------------
-- These three are internal. They are never granted to any service role, and
-- they are SECURITY INVOKER, so even if EXECUTE leaked they would return
-- nothing to a caller that cannot read the underlying tables. They exist as
-- separate functions so that P2-MEM-3's timeline and P1-AGT-2's graph
-- expansion reuse this exact ACL logic instead of restating it.
--
-- search_path is pinned on every function here. An unpinned SECURITY DEFINER
-- function is a privilege-escalation hole, and 002 already revoked CREATE on
-- schema public from PUBLIC and from all three service roles, so nothing
-- untrusted can plant a shadowing object.

-- Transitive group closure. UNION, not UNION ALL: membership cycles are bad
-- data rather than a reason to hang, and deduplication terminates them.
CREATE FUNCTION _expanded_principals(p_principal uuid)
RETURNS TABLE (principal_id uuid)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE closure(principal_id) AS (
        SELECT p_principal
        UNION
        SELECT pm.group_id
        FROM principal_memberships pm
        JOIN closure c ON c.principal_id = pm.member_id
    )
    SELECT closure.principal_id FROM closure;
$$;

-- The ACL half.
CREATE FUNCTION _visible_entity_ids(p_principal uuid)
RETURNS TABLE (entity_id uuid)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT DISTINCT g.entity_id
    FROM acl_grants g
    WHERE g.principal_id IN (SELECT ep.principal_id FROM _expanded_principals(p_principal) ep);
$$;

-- The scope half. A personal scope is owned by a user, and a user is in the
-- closure only when it is the asking principal itself, so one containment test
-- covers both team and personal correctly.
CREATE FUNCTION _visible_scope_ids(p_principal uuid)
RETURNS TABLE (scope_id uuid)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT s.id
    FROM memory_scopes s
    WHERE s.scope_type = 'org'
       OR s.owner_principal IN (SELECT ep.principal_id FROM _expanded_principals(p_principal) ep);
$$;

-- ------------------------------------------------------------
-- The one granted entry point
-- ------------------------------------------------------------
-- Modes, fused with reciprocal rank fusion:
--   vector  when p_query_embedding is given
--   fts     when p_query_text is given
--   graph   when p_expand_hops > 0, walking out from the hits above
--   browse  when neither query input is given, so callers that want "what can
--           this principal see" have a supported way to ask
--
-- Every mode draws from `candidates`, which is already intersected with both
-- halves of the predicate. Graph expansion additionally walks *through* visible
-- entities only, so a traversal cannot use an invisible entity as a stepping
-- stone or reveal that one exists.
--
-- Performance note, stated honestly: filtering before the vector search means
-- the HNSW index is used under a post-filter, which degrades as the visible set
-- shrinks relative to the corpus. pgvector 0.8's iterative index scans
-- (hnsw.iterative_scan) are the intended remedy and are a session GUC, not a
-- schema decision. STACK.md's graduation trigger for vectors is measured recall
-- and latency, and P2-EVAL-1 is where that measurement lives. Correctness is
-- not negotiable here; speed has an exit.
CREATE FUNCTION visible_chunks(
    p_principal       uuid,
    p_query_text      text DEFAULT NULL,
    p_query_embedding vector(1024) DEFAULT NULL,
    p_k               int DEFAULT 20,
    p_expand_hops     int DEFAULT 0
)
RETURNS TABLE (
    chunk_id       uuid,
    entity_id      uuid,
    entity_type    text,
    entity_title   text,
    scope_id       uuid,
    content        text,
    score          double precision,
    retrieval_modes text[]
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE
    limits AS (
        SELECT
            GREATEST(COALESCE(p_k, 20), 1)                        AS k,
            LEAST(GREATEST(COALESCE(p_expand_hops, 0), 0), 2)     AS hops
    ),
    visible_entities AS (
        SELECT ve.entity_id FROM _visible_entity_ids(p_principal) ve
    ),
    visible_scopes AS (
        SELECT vs.scope_id FROM _visible_scope_ids(p_principal) vs
    ),
    -- Everything downstream selects from here and only from here.
    candidates AS (
        SELECT c.id, c.entity_id, c.scope_id, c.content, c.embedding, c.content_tsv
        FROM chunks c
        JOIN visible_entities ve ON ve.entity_id = c.entity_id
        JOIN visible_scopes vs ON vs.scope_id = c.scope_id
    ),
    vector_hits AS (
        SELECT t.id, row_number() OVER (ORDER BY t.distance) AS rank
        FROM (
            SELECT c.id, c.embedding <=> p_query_embedding AS distance
            FROM candidates c, limits l
            WHERE p_query_embedding IS NOT NULL AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> p_query_embedding
            LIMIT (SELECT k * 4 FROM limits)
        ) t
    ),
    fts_hits AS (
        SELECT t.id, row_number() OVER (ORDER BY t.relevance DESC) AS rank
        FROM (
            SELECT c.id, ts_rank_cd(c.content_tsv, q.query) AS relevance
            FROM candidates c, plainto_tsquery('english', p_query_text) AS q(query)
            WHERE p_query_text IS NOT NULL AND c.content_tsv @@ q.query
            ORDER BY ts_rank_cd(c.content_tsv, q.query) DESC
            LIMIT (SELECT k * 4 FROM limits)
        ) t
    ),
    browse_hits AS (
        SELECT c.id, row_number() OVER (ORDER BY c.id) AS rank
        FROM candidates c
        WHERE p_query_text IS NULL AND p_query_embedding IS NULL
    ),
    seeds AS (
        SELECT DISTINCT c.entity_id
        FROM candidates c
        WHERE c.id IN (SELECT vh.id FROM vector_hits vh UNION SELECT fh.id FROM fts_hits fh)
    ),
    -- Only visible entities are ever added to the walk.
    walk (entity_id, hops) AS (
        SELECT s.entity_id, 0 FROM seeds s
        UNION
        SELECT ve.entity_id, w.hops + 1
        FROM walk w
        JOIN edges e ON e.src_id = w.entity_id OR e.dst_id = w.entity_id
        JOIN visible_entities ve
          ON ve.entity_id = CASE WHEN e.src_id = w.entity_id THEN e.dst_id ELSE e.src_id END
        WHERE w.hops < (SELECT hops FROM limits)
    ),
    nearest_hop AS (
        SELECT w.entity_id, min(w.hops) AS hops FROM walk w GROUP BY w.entity_id
    ),
    graph_hits AS (
        SELECT c.id, nh.hops
        FROM candidates c
        JOIN nearest_hop nh ON nh.entity_id = c.entity_id
        WHERE nh.hops > 0
    ),
    contributions AS (
        SELECT vh.id, 1.0 / (60 + vh.rank) AS weight, 'vector' AS mode FROM vector_hits vh
        UNION ALL
        SELECT fh.id, 1.0 / (60 + fh.rank), 'fts' FROM fts_hits fh
        UNION ALL
        SELECT bh.id, 1.0 / (60 + bh.rank), 'browse' FROM browse_hits bh
        UNION ALL
        SELECT gh.id, 1.0 / (60 + 10 * gh.hops), 'graph' FROM graph_hits gh
    ),
    fused AS (
        SELECT
            con.id,
            sum(con.weight)::double precision AS score,
            array_agg(DISTINCT con.mode ORDER BY con.mode) AS modes
        FROM contributions con
        GROUP BY con.id
    )
    SELECT
        c.id,
        c.entity_id,
        e.entity_type,
        e.title,
        c.scope_id,
        c.content,
        f.score,
        f.modes
    FROM fused f
    JOIN candidates c ON c.id = f.id
    JOIN entities e ON e.id = c.entity_id
    ORDER BY f.score DESC, c.id
    LIMIT (SELECT k FROM limits);
$$;

-- ------------------------------------------------------------
-- Grants
-- ------------------------------------------------------------
-- CREATE FUNCTION grants EXECUTE to PUBLIC by default. Undo that on all four
-- before granting anything, or the filter would be bypassable by every role in
-- the cluster.
REVOKE ALL ON FUNCTION _expanded_principals(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION _visible_entity_ids(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION _visible_scope_ids(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION visible_chunks(uuid, text, vector, int, int) FROM PUBLIC;

-- 001 created this trigger function before the grant discipline existed, and it
-- picked up the same default PUBLIC grant. Trigger firing does not consult
-- EXECUTE, so revoking costs nothing and keeps every service role's function
-- surface to exactly what was granted on purpose.
REVOKE ALL ON FUNCTION set_updated_at() FROM PUBLIC;

-- The agent's entire read surface, now and permanently.
GRANT EXECUTE ON FUNCTION visible_chunks(uuid, text, vector, int, int) TO hippo_agent;
