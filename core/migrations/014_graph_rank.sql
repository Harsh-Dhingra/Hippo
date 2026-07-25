-- ============================================================
-- Hippo — Graph expansion that discriminates (P2-EVAL-1)
--
-- Found by the eval harness, which is what it is for.
--
-- On a 432-chunk seeded graph, traversal recall was 0.00 at every usable k.
-- Not because expansion failed to reach the right chunk — the walk found it
-- one hop away, correctly — but because it also reached fifty others and
-- scored them all identically.
--
-- The cause: every vector and FTS hit becomes a seed, and the vector mode
-- returns k * 4 of them. So a neighbour of the eightieth-best match was
-- weighted exactly like a neighbour of the best one, at a flat
-- 1 / (60 + 10 * hops). Graph expansion was contributing volume rather than
-- signal, and the genuinely relevant neighbour was buried under its own
-- siblings — every other message in the same channel is one hop away through
-- the container.
--
-- The fix is the one reciprocal rank fusion already implies: a neighbour
-- inherits the standing of whatever pulled it in. The seed's rank rides along
-- the walk, and the weight becomes
--
--     1 / (60 + seed_rank + 10 * hops)
--
-- so a reply to the top hit scores 1/71 while a sibling of the eightieth
-- scores 1/150. Same walk, same cost, ordered by how good a reason there was
-- to look there.
--
-- This is a ranking change and not a visibility change. Every join to
-- visible_entities is untouched, nothing new becomes reachable, and the
-- permission property suite and the red team both re-run against it.
-- ============================================================

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
    -- A seed carries how well it matched, not just that it matched.
    seeds AS (
        SELECT c.entity_id, min(t.rank) AS seed_rank
        FROM candidates c
        JOIN (
            SELECT vh.id, vh.rank FROM vector_hits vh
            UNION ALL
            SELECT fh.id, fh.rank FROM fts_hits fh
        ) t ON t.id = c.id
        GROUP BY c.entity_id
    ),
    -- The seed's rank rides along, so a neighbour inherits the standing of
    -- whatever pulled it in.
    walk (entity_id, hops, seed_rank) AS (
        SELECT s.entity_id, 0, s.seed_rank FROM seeds s
        UNION
        SELECT ve.entity_id, w.hops + 1, w.seed_rank
        FROM walk w
        JOIN edges e ON e.src_id = w.entity_id OR e.dst_id = w.entity_id
        JOIN visible_entities ve
          ON ve.entity_id = CASE WHEN e.src_id = w.entity_id THEN e.dst_id ELSE e.src_id END
        WHERE w.hops < (SELECT hops FROM limits)
    ),
    nearest_hop AS (
        SELECT w.entity_id, min(w.hops) AS hops, min(w.seed_rank) AS seed_rank
        FROM walk w GROUP BY w.entity_id
    ),
    graph_hits AS (
        SELECT c.id, nh.hops, nh.seed_rank
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
        SELECT gh.id, 1.0 / (60 + gh.seed_rank + 10 * gh.hops), 'graph' FROM graph_hits gh
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
