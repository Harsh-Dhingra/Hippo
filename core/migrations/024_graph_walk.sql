-- ============================================================
-- Hippo — The walk, made typed, bounded and directional (P3-GRF-1)
--
-- The graph is the differentiator, and until now it has been contributing
-- almost nothing and would have stopped working entirely at scale. Three
-- problems, and one idea fixes all three.
--
-- WHAT WAS MEASURED
--
--     k=3    graph=0.000    traversal=0.00
--     k=10   graph=0.000    traversal=0.00
--     k=20   graph=0.250    traversal=0.75
--
-- A neighbour scored 1/(60 + seed_rank + 10*hops), so a one-hop neighbour of
-- the best-matching chunk sorted below the tenth direct hit. At any realistic
-- context budget the graph half of hybrid retrieval was decoration.
--
-- WHAT WOULD HAVE HAPPENED AT SCALE
--
-- The walk joined `edges` on `src_id = w.entity_id OR dst_id = w.entity_id`:
-- undirected, untyped, unbounded. A seed message reached its author in one hop
-- and *every message that author had ever written* in two. On a real corpus a
-- two-hop walk from anybody busy is most of the workspace — slow, and full of
-- things that have nothing to do with the question.
--
-- DIRECTION IS THE WHOLE IDEA
--
-- Both problems are the same problem. Every explosion runs from the
-- low-cardinality side of an edge to the high-cardinality side: a person to
-- their messages, a channel to its contents. Every *useful* traversal runs the
-- other way: a message to its author, a message to its channel.
--
-- So an edge is traversable per direction, and the dangerous direction is
-- simply not traversable. That bounds the fan-out by construction rather than
-- by a cap that has to be tuned, and it is also what makes weighting
-- meaningful: "message → its author" and "author → all their messages" are
-- different relationships that happen to share a row.
--
-- WHY THE RECURSION IS UNROLLED
--
-- hops has always been capped at two. Postgres allows neither LIMIT nor window
-- functions in the recursive term of a WITH RECURSIVE, so a bounded fan-out
-- was not expressible there. Two explicit hops are: each one can rank its
-- neighbours and keep the best, which is the backstop for the one remaining
-- direction that is legitimately unbounded (a thread with ten thousand
-- replies).
-- ============================================================

-- ------------------------------------------------------------
-- What each relationship is worth, and which way it runs
-- ------------------------------------------------------------
-- A weight of zero means "never traverse this way". That is not a ranking
-- decision, it is the fan-out bound: the zeroes are exactly the person → their
-- work and container → its contents directions.
--
-- A function rather than a table, deliberately. These weights change what
-- every answer contains, so changing them should be a migration somebody
-- reviews — not a row somebody can edit at three in the morning.
CREATE FUNCTION _edge_weight(p_edge_type text, p_forward boolean)
RETURNS double precision
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE p_edge_type
        -- Two entities that *are* the same thing. A neighbour across this edge
        -- is as relevant as what pulled it in, in either direction.
        WHEN 'same_as'     THEN 0.95

        -- A conversation. The reply to the message that matched is very often
        -- the answer, and a thread is bounded, so both directions are worth
        -- walking. Reverse slightly lower only because one parent may have
        -- many replies and most of them are not the one.
        WHEN 'replies_to'  THEN CASE WHEN p_forward THEN 0.85 ELSE 0.60 END

        -- Dependency. The strongest signal there is for "what is blocking X",
        -- which is the question this product exists to answer. Nothing creates
        -- these yet — P3-GRF-2 does — and the weights are here first so that
        -- when they arrive they are already worth walking.
        WHEN 'blocks'      THEN CASE WHEN p_forward THEN 0.90 ELSE 0.90 END
        WHEN 'resolved_by' THEN CASE WHEN p_forward THEN 0.90 ELSE 0.80 END
        WHEN 'references'  THEN CASE WHEN p_forward THEN 0.70 ELSE 0.50 END

        -- Containment: src is the object, dst is the container. Upward is
        -- cheap and mildly useful — the channel tells you a little about the
        -- message. Downward is a channel to its ten thousand messages, which
        -- is the explosion, so it is not traversable at all.
        WHEN 'belongs_to'  THEN CASE WHEN p_forward THEN 0.25 ELSE 0.0 END

        -- Authorship: src is the person, dst is the thing. Forward is a person
        -- to everything they ever wrote — the other explosion, and the one
        -- that made a two-hop walk unusable. Backward is "who wrote this",
        -- which is one row and worth something.
        WHEN 'authored'    THEN CASE WHEN p_forward THEN 0.0 ELSE 0.40 END

        -- src is the message, dst is the person mentioned. Forward is a few
        -- people. Backward is every message that ever named them.
        WHEN 'mentions'    THEN CASE WHEN p_forward THEN 0.30 ELSE 0.0 END

        -- src is the ticket, dst is the assignee. Same shape.
        WHEN 'assigned_to' THEN CASE WHEN p_forward THEN 0.35 ELSE 0.0 END

        -- An edge type nobody has weighted yet. Traversable, weakly, forward
        -- only: a new relationship should be able to help without a migration,
        -- and should not be able to explode without one.
        ELSE CASE WHEN p_forward THEN 0.20 ELSE 0.0 END
    END;
$$;

COMMENT ON FUNCTION _edge_weight(text, boolean) IS
    'How much a neighbour keeps of the standing of whatever reached it, per '
    'direction. Zero means never traverse: those are the person-to-their-work '
    'and container-to-its-contents directions, and they are the fan-out bound.';

REVOKE ALL ON FUNCTION _edge_weight(text, boolean) FROM PUBLIC;

-- Both directions of every traversable edge, as rows. Written once here so the
-- two hops below are the same query twice rather than two things to keep in
-- step.
CREATE FUNCTION _edge_steps()
RETURNS TABLE (from_id uuid, to_id uuid, edge_type text, weight double precision)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT e.src_id, e.dst_id, e.edge_type,
           _edge_weight(e.edge_type, true) * e.confidence
    FROM edges e
    WHERE _edge_weight(e.edge_type, true) > 0
    UNION ALL
    SELECT e.dst_id, e.src_id, e.edge_type,
           _edge_weight(e.edge_type, false) * e.confidence
    FROM edges e
    WHERE _edge_weight(e.edge_type, false) > 0;
$$;

COMMENT ON FUNCTION _edge_steps() IS
    'Traversable edges, one row per usable direction, weighted by type and by '
    'the edge''s own confidence — so a model-inferred edge is worth less than '
    'the same relationship stated by a source.';

REVOKE ALL ON FUNCTION _edge_steps() FROM PUBLIC;

-- ------------------------------------------------------------
-- The filter
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
