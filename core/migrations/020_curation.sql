-- ============================================================
-- Hippo — Curation: decay, noise, supersede (P2-MEM-2)
--
-- PROJECT.md calls this the gap in everyone else, so it is worth being precise
-- about what it is and what it deliberately is not.
--
-- EVERYTHING HERE IS RANKING. NOTHING HERE IS VISIBILITY.
--
-- That is the whole design constraint. A curation pass that hid content would
-- be a system that quietly stopped answering questions it could answer, and
-- the person asking would have no way to tell the difference between "we do not
-- have that" and "we decided it was stale". So a demoted chunk still comes back
-- when nothing better exists, still appears in the timeline, and still counts
-- in the audit trail.
--
-- It also keeps the permission property suite meaningful: the filter's visible
-- set is unchanged, so the 10k-case oracle still describes it exactly.
--
-- THREE MULTIPLIERS
--
-- Recency, on a long half-life. A year-old message is not wrong, it is just
-- less likely to be current, and the curve is gentle enough to settle ties
-- rather than to bury an old exact match under a new vague one. Content the
-- source never dated is unpenalised — absence of a timestamp is not evidence
-- of age, and migration 016 was careful not to invent one.
--
-- Noise, from the curation pass. "+1", "thanks", a lunch order: real messages
-- that will never answer a question, currently occupying retrieval slots that
-- something useful wanted. Defaulting to 1.0 means nothing is demoted until
-- something has actually assessed it.
--
-- Supersede, at a quarter weight rather than zero. A superseded fact is still
-- true history — "our floor was 18 percent" was correct in June — and the
-- timeline is built on exactly that. It should lose to the current statement,
-- not vanish.
-- ============================================================

ALTER TABLE chunks ADD COLUMN signal real NOT NULL DEFAULT 1.0;
ALTER TABLE chunks ADD COLUMN superseded_by uuid REFERENCES chunks(id) ON DELETE SET NULL;
ALTER TABLE chunks ADD COLUMN curated_at timestamptz;

ALTER TABLE chunks ADD CONSTRAINT chunks_signal_is_a_weight
    CHECK (signal >= 0.0 AND signal <= 1.0);
ALTER TABLE chunks ADD CONSTRAINT chunks_cannot_supersede_itself
    CHECK (superseded_by IS NULL OR superseded_by <> id);

CREATE INDEX chunks_uncurated_idx ON chunks (curated_at) WHERE curated_at IS NULL;
CREATE INDEX chunks_superseded_idx ON chunks (superseded_by) WHERE superseded_by IS NOT NULL;

COMMENT ON COLUMN chunks.signal IS
    'How likely this chunk is to ever answer a question. A ranking weight, not '
    'a filter: 0.0 still comes back when nothing better exists.';
COMMENT ON COLUMN chunks.superseded_by IS
    'A later chunk stating the same fact. Demoted rather than hidden, because a '
    'superseded fact is still true history and the timeline is built on it.';

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
    -- Curation (P2-MEM-2). Three multipliers, all of them ranking and none of
    -- them visibility: a demoted chunk is still returned when nothing better
    -- exists, and is still in the timeline and the audit trail. Hiding content
    -- on a quality heuristic is how a search tool starts lying about what it
    -- has.
    curated AS (
        SELECT
            f.id,
            f.score
                -- Recency. A gentle curve with a long half-life, so it settles
                -- ties between comparably relevant chunks rather than burying
                -- an old exact match under a new vague one. Undated content is
                -- unpenalised: absence of a timestamp is not evidence of age.
                * CASE
                    WHEN e.occurred_at IS NULL THEN 1.0
                    ELSE 1.0 / (1.0 + (extract(epoch FROM now() - e.occurred_at)
                                       / 86400.0) / 365.0)
                  END
                -- Noise. "+1", "thanks", a lunch order. Computed by the
                -- curation pass, defaulting to 1.0 so nothing is demoted until
                -- something has actually looked at it.
                * c.signal
                -- Superseded. Still true history and still retrievable, but a
                -- later statement of the same fact should win.
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

REVOKE ALL ON FUNCTION visible_chunks(uuid, text, vector, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION visible_chunks(uuid, text, vector, int, int) TO hippo_agent;
