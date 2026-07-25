-- ============================================================
-- Hippo — One human, several accounts (P1-AGT-2)
--
-- Two problems, both found by ARCHITECTURE §12 point 1: a cited answer
-- spanning a Slack thread and a Jira ticket.
--
-- 1. THE ASKING USER IS ONE PERSON WITH SEVERAL ACCOUNTS.
--
-- P1-RES-2 merges Alice's Slack and Jira records into one *entity*, but
-- principals stay per-connector, and correctly so: a grant is asserted by a
-- source system about an account in that source system. The consequence was
-- that Alice-in-Slack could not see anything Jira had granted to
-- Alice-in-Jira, so no single asker could ever get an answer spanning both
-- systems. The demo is impossible without this.
--
-- The fix mirrors the rule identity resolution already uses on entities: two
-- accounts with the same email are one human. principals.identity_id records
-- that, and the principal closure starts from every account sharing it before
-- walking group membership upward.
--
-- This widens visibility, so it is exactly the change the permission property
-- suite exists to check. Note what it does not do: it never merges accounts
-- without an email, and it never crosses from a user to a group.
--
-- 2. KEYWORD SEARCH REQUIRED EVERY WORD OF THE QUESTION.
--
-- plainto_tsquery ANDs its lexemes, so "What is ACME-1 about?" only matched
-- chunks containing all of "acme-1", "acme" and "1". Real questions are a bag
-- of terms, not a conjunctive filter, and the effect was that keyword search
-- silently contributed almost nothing — the exact-identifier case P1-AGT-2
-- exists to prove would have been carried entirely by vector search.
--
-- Relaxing the conjunction to a disjunction keeps plainto_tsquery's stemming
-- and stop-word handling and lets ts_rank_cd do the discriminating, which is
-- what ranking is for.
-- ============================================================

ALTER TABLE principals ADD COLUMN identity_id uuid;

-- Only a human holds several accounts. A shared identity_id on two *groups*
-- would hand every member of one the grants of the other, so the closure below
-- does not have to guard against it: the schema does.
ALTER TABLE principals ADD CONSTRAINT principals_identity_is_for_users
    CHECK (identity_id IS NULL OR kind = 'user');

CREATE INDEX principals_identity_idx ON principals (identity_id)
    WHERE identity_id IS NOT NULL;

COMMENT ON COLUMN principals.identity_id IS
    'Shared by every account belonging to one human. Assigned by identity '
    'resolution on the same rule it merges person entities with: a matching '
    'email. NULL means this account stands alone.';

-- ------------------------------------------------------------
-- The principal closure, now spanning a person's accounts
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION _expanded_principals(p_principal uuid)
RETURNS TABLE (principal_id uuid)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE
    -- Every account this human holds, including the one that asked. A
    -- principal with no identity_id is only ever itself.
    seed AS (
        SELECT p_principal AS principal_id
        UNION
        SELECT sibling.id
        FROM principals me
        JOIN principals sibling ON sibling.identity_id = me.identity_id
        WHERE me.id = p_principal AND me.identity_id IS NOT NULL
    ),
    -- Then upward through group membership, from all of them.
    closure(principal_id) AS (
        SELECT s.principal_id FROM seed s
        UNION
        SELECT pm.group_id
        FROM principal_memberships pm
        JOIN closure c ON c.principal_id = pm.member_id
    )
    SELECT closure.principal_id FROM closure;
$$;

-- ------------------------------------------------------------
-- Keyword retrieval that a question can actually match
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION visible_chunks(
    p_principal       uuid,
    p_query_text      text DEFAULT NULL,
    p_query_embedding vector(1024) DEFAULT NULL,
    p_k               int DEFAULT 20,
    p_expand_hops     int DEFAULT 0
)
RETURNS TABLE (
    chunk_id           uuid,
    entity_id          uuid,
    entity_type        text,
    entity_title       text,
    scope_id           uuid,
    content            text,
    score              double precision,
    retrieval_modes    text[],
    connector_id       uuid,
    source_type        text,
    source_id          text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE
    limits AS (
        SELECT
            GREATEST(COALESCE(p_k, 20), 1)                    AS k,
            LEAST(GREATEST(COALESCE(p_expand_hops, 0), 0), 2)  AS hops
    ),
    -- plainto_tsquery does the stemming and stop-word removal, then the
    -- conjunction is relaxed so a question matches on any of its terms.
    query AS (
        SELECT nullif(
                   replace(plainto_tsquery('english', p_query_text)::text, ' & ', ' | '),
                   ''
               )::tsquery AS ts
        WHERE p_query_text IS NOT NULL
    ),
    visible_entities AS (
        SELECT ve.entity_id FROM _visible_entity_ids(p_principal) ve
    ),
    visible_scopes AS (
        SELECT vs.scope_id FROM _visible_scope_ids(p_principal) vs
    ),
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
            SELECT c.id, ts_rank_cd(c.content_tsv, q.ts) AS relevance
            FROM candidates c, query q
            WHERE q.ts IS NOT NULL AND c.content_tsv @@ q.ts
            ORDER BY ts_rank_cd(c.content_tsv, q.ts) DESC
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
    ),
    citation AS (
        SELECT DISTINCT ON (es.entity_id)
               es.entity_id, r.connector_id, r.source_type, r.source_id
        FROM entity_sources es
        JOIN raw_records r ON r.id = es.raw_record_id
        ORDER BY es.entity_id, r.source_type, r.source_id
    )
    SELECT
        c.id,
        c.entity_id,
        e.entity_type,
        e.title,
        c.scope_id,
        c.content,
        f.score,
        f.modes,
        cit.connector_id,
        cit.source_type,
        cit.source_id
    FROM fused f
    JOIN candidates c ON c.id = f.id
    JOIN entities e ON e.id = c.entity_id
    LEFT JOIN citation cit ON cit.entity_id = c.entity_id
    ORDER BY f.score DESC, c.id
    LIMIT (SELECT k FROM limits);
$$;

REVOKE ALL ON FUNCTION visible_chunks(uuid, text, vector, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION visible_chunks(uuid, text, vector, int, int) TO hippo_agent;
