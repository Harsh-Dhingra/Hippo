-- Reverses 025 by restoring the 024 walk, hash join and all.

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
    WITH
    limits AS (
        SELECT
            GREATEST(COALESCE(p_k, 20), 1)                    AS k,
            LEAST(GREATEST(COALESCE(p_expand_hops, 0), 0), 2)  AS hops,
            -- How many neighbours one entity may pull in per hop. The
            -- direction weights above remove the explosions; this bounds the
            -- one direction that is legitimately unbounded, a thread with
            -- thousands of replies. Generous enough that no real conversation
            -- is truncated.
            25                                                 AS fan_out
    ),
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
        SELECT c.entity_id, min(t.rank) AS seed_rank
        FROM candidates c
        JOIN (
            SELECT vh.id, vh.rank FROM vector_hits vh
            UNION ALL
            SELECT fh.id, fh.rank FROM fts_hits fh
        ) t ON t.id = c.id
        GROUP BY c.entity_id
    ),
    -- Hop one. `strength` is what a neighbour keeps of its seed's standing,
    -- and the fan-out cap keeps the best few per seed rather than all of them.
    hop_1 AS (
        SELECT entity_id, seed_rank, strength FROM (
            SELECT
                ve.entity_id,
                s.seed_rank,
                st.weight AS strength,
                row_number() OVER (
                    PARTITION BY s.entity_id ORDER BY st.weight DESC, ve.entity_id
                ) AS fan
            FROM seeds s
            JOIN _edge_steps() st ON st.from_id = s.entity_id
            JOIN visible_entities ve ON ve.entity_id = st.to_id
            WHERE (SELECT hops FROM limits) >= 1
        ) ranked
        WHERE fan <= (SELECT fan_out FROM limits)
    ),
    -- Hop two, from hop one. Strength multiplies, so two weak edges reach much
    -- further down the ranking than one strong one — which is the point: a
    -- neighbour of a neighbour is only interesting when both steps were.
    hop_2 AS (
        SELECT entity_id, seed_rank, strength FROM (
            SELECT
                ve.entity_id,
                h.seed_rank,
                h.strength * st.weight AS strength,
                row_number() OVER (
                    PARTITION BY h.entity_id ORDER BY st.weight DESC, ve.entity_id
                ) AS fan
            FROM hop_1 h
            JOIN _edge_steps() st ON st.from_id = h.entity_id
            JOIN visible_entities ve ON ve.entity_id = st.to_id
            WHERE (SELECT hops FROM limits) >= 2
        ) ranked
        WHERE fan <= (SELECT fan_out FROM limits)
    ),
    -- One row per reached entity, keeping its best route in. `min(seed_rank)`
    -- and `max(strength)` are chosen independently on purpose: an entity
    -- reached weakly from a great seed and strongly from a poor one deserves
    -- the better of each.
    reached AS (
        SELECT w.entity_id, min(w.seed_rank) AS seed_rank, max(w.strength) AS strength
        FROM (
            SELECT entity_id, seed_rank, strength FROM hop_1
            UNION ALL
            SELECT entity_id, seed_rank, strength FROM hop_2
        ) w
        WHERE w.entity_id NOT IN (SELECT s.entity_id FROM seeds s)
        GROUP BY w.entity_id
    ),
    graph_hits AS (
        SELECT c.id, r.seed_rank, r.strength
        FROM candidates c
        JOIN reached r ON r.entity_id = c.entity_id
    ),
    contributions AS (
        SELECT vh.id, 1.0 / (60 + vh.rank) AS weight, 'vector' AS mode FROM vector_hits vh
        UNION ALL
        SELECT fh.id, 1.0 / (60 + fh.rank), 'fts' FROM fts_hits fh
        UNION ALL
        SELECT bh.id, 1.0 / (60 + bh.rank), 'browse' FROM browse_hits bh
        UNION ALL
        -- Strength decides how many rank places the hop costs, rather than
        -- scaling the contribution. Reciprocal-rank fusion is nearly flat —
        -- every contribution sits between 1/61 and 1/80 — so a 0.6 multiplier
        -- there is not "slightly worse", it is thirty places worse. Dividing
        -- into the penalty instead keeps the calibration legible and matches
        -- what the weights are supposed to mean:
        --
        --     same_as     0.95  ->  ~4 places behind its seed
        --     replies_to  0.60  ->  ~7
        --     authored    0.40  ->  ~10
        --     belongs_to  0.25  ->  ~16
        --
        -- Two hops multiply their strengths, so the cost of a weak second step
        -- compounds rather than adding — which is the intent: a neighbour of a
        -- neighbour is only interesting when both steps were.
        SELECT gh.id, 1.0 / (60 + gh.seed_rank + 4.0 / gh.strength), 'graph'
        FROM graph_hits gh
    ),
    fused AS (
        SELECT
            con.id,
            sum(con.weight)::double precision AS score,
            array_agg(DISTINCT con.mode ORDER BY con.mode) AS modes
        FROM contributions con
        GROUP BY con.id
    ),
    curated AS (
        SELECT
            f.id,
            f.score
                * CASE
                    WHEN e.occurred_at IS NULL THEN 1.0
                    ELSE 1.0 / (1.0 + (extract(epoch FROM now() - e.occurred_at)
                                       / 86400.0) / 365.0)
                  END
                * c.signal
                * CASE WHEN c.superseded_by IS NULL THEN 1.0 ELSE 0.25 END
                AS score,
            f.modes
        FROM fused f
        JOIN chunks c ON c.id = f.id
        JOIN entities e ON e.id = c.entity_id
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
    FROM curated f
    JOIN candidates c ON c.id = f.id
    JOIN entities e ON e.id = c.entity_id
    LEFT JOIN citation cit ON cit.entity_id = c.entity_id
    ORDER BY f.score DESC, c.id
    LIMIT (SELECT k FROM limits);
$$;
