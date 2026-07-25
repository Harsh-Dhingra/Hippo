-- Reverts 007_visible_chunks_sources.sql by restoring the 003 definition.
--
-- The function body is duplicated here rather than referenced, because a down
-- migration that depends on reading another migration's file is a down
-- migration that breaks when that file moves.

DROP FUNCTION IF EXISTS visible_chunks(uuid, text, vector, int, int);

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
            GREATEST(COALESCE(p_k, 20), 1)                    AS k,
            LEAST(GREATEST(COALESCE(p_expand_hops, 0), 0), 2)  AS hops
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
        c.id, c.entity_id, e.entity_type, e.title, c.scope_id, c.content, f.score, f.modes
    FROM fused f
    JOIN candidates c ON c.id = f.id
    JOIN entities e ON e.id = c.entity_id
    ORDER BY f.score DESC, c.id
    LIMIT (SELECT k FROM limits);
$$;

REVOKE ALL ON FUNCTION visible_chunks(uuid, text, vector, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION visible_chunks(uuid, text, vector, int, int) TO hippo_agent;
