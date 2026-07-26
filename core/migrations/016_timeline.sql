-- ============================================================
-- Hippo — The Memory Timeline (P2-MEM-3)
--
-- "Reconstruct the causal/temporal chain around any entity — pricing change,
-- Slack thread, PR, deploy, complaint, ticket, fix — from timestamped
-- entities and edges."
--
-- WHEN SOMETHING HAPPENED IS NOT WHEN WE HEARD ABOUT IT
--
-- entities.created_at is when the resolver first saw a record. Every entity in
-- a freshly synced workspace shares roughly one created_at, because that is
-- when the sync ran. A timeline built on it would draw a flat line at import
-- time and call it history.
--
-- So occurred_at is a separate column, taken from what the source says: a
-- Slack message's ts, a Jira issue's created. It is nullable, and deliberately
-- so — an entity whose source states no time has no time, and guessing one
-- from the sync clock would be inventing history rather than reporting it.
-- Those entities are the connective tissue of a timeline (people, channels,
-- projects) and appear as context rather than as events.
--
-- PROJECT.md says "query + UI over existing schema, no new storage". One
-- column on an existing table is the smallest thing that makes the query
-- possible at all; a timeline with no time is a list.
--
-- PERMISSION-FILTERED PER VIEWER
--
-- A timeline is a new way to see what exists, so it is a new way to leak. It
-- reuses _visible_entity_ids, which is the same ACL predicate the retrieval
-- filter uses for graph expansion — so an entity appears in your timeline
-- exactly when it could already appear in your answers. No new rule to get
-- wrong, and the red team's probes apply unchanged.
-- ============================================================

ALTER TABLE entities ADD COLUMN occurred_at timestamptz;

COMMENT ON COLUMN entities.occurred_at IS
    'When the source says this happened, not when we synced it. NULL when the '
    'source states no time — guessing from the sync clock would invent history.';

CREATE INDEX entities_occurred_idx ON entities (occurred_at DESC NULLS LAST)
    WHERE occurred_at IS NOT NULL;

-- ------------------------------------------------------------
-- Backfill from what has already been synced
-- ------------------------------------------------------------
-- Slack sends an epoch string; Jira an ISO timestamp. Both are read from
-- raw_records, which is source truth and immutable — this reads it and writes
-- the graph, exactly as the resolver does.
UPDATE entities e
SET occurred_at = to_timestamp(split_part(r.payload ->> 'ts', '.', 1)::bigint)
FROM entity_sources es
JOIN raw_records r ON r.id = es.raw_record_id
WHERE es.entity_id = e.id
  AND e.occurred_at IS NULL
  AND r.source_type = 'slack.message'
  AND r.payload ->> 'ts' ~ '^[0-9]+(\.[0-9]+)?$';

UPDATE entities e
SET occurred_at = (r.payload -> 'fields' ->> 'created')::timestamptz
FROM entity_sources es
JOIN raw_records r ON r.id = es.raw_record_id
WHERE es.entity_id = e.id
  AND e.occurred_at IS NULL
  AND r.source_type = 'jira.issue'
  AND r.payload -> 'fields' ->> 'created' IS NOT NULL;

UPDATE entities e
SET occurred_at = (r.payload ->> 'created')::timestamptz
FROM entity_sources es
JOIN raw_records r ON r.id = es.raw_record_id
WHERE es.entity_id = e.id
  AND e.occurred_at IS NULL
  AND r.source_type = 'jira.comment'
  AND r.payload ->> 'created' IS NOT NULL;

-- ------------------------------------------------------------
-- The timeline
-- ------------------------------------------------------------
CREATE FUNCTION timeline(
    p_principal uuid,
    p_entity    uuid,
    p_hops      int DEFAULT 2,
    p_limit     int DEFAULT 100
)
RETURNS TABLE (
    entity_id     uuid,
    entity_type   text,
    title         text,
    occurred_at   timestamptz,
    hops          int,
    via           text,
    connector_id  uuid,
    source_type   text,
    source_id     text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH RECURSIVE
    limits AS (
        SELECT LEAST(GREATEST(COALESCE(p_hops, 2), 1), 3) AS hops,
               LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500) AS cap
    ),
    -- The same ACL predicate the retrieval filter uses for graph expansion, so
    -- an entity appears here exactly when it could appear in an answer.
    visible AS (
        SELECT ve.entity_id FROM _visible_entity_ids(p_principal) ve
    ),
    walk (entity_id, hops, via) AS (
        SELECT v.entity_id, 0, NULL::text
        FROM visible v
        WHERE v.entity_id = p_entity
        UNION
        SELECT nxt.entity_id, w.hops + 1, e.edge_type
        FROM walk w
        JOIN edges e ON e.src_id = w.entity_id OR e.dst_id = w.entity_id
        JOIN visible nxt
          ON nxt.entity_id = CASE WHEN e.src_id = w.entity_id THEN e.dst_id ELSE e.src_id END
        WHERE w.hops < (SELECT hops FROM limits)
    ),
    nearest AS (
        SELECT w.entity_id,
               min(w.hops) AS hops,
               -- The edge type from the shortest path in, which is what makes
               -- a row readable as "a reply to" rather than "somehow related".
               (array_agg(w.via ORDER BY w.hops))[1] AS via
        FROM walk w
        GROUP BY w.entity_id
    ),
    -- One source reference per entity, for the citation. Same shape the
    -- retrieval filter uses, and for the same reason: a timeline entry nobody
    -- can click through to is an assertion rather than evidence.
    citation AS (
        SELECT DISTINCT ON (es.entity_id)
               es.entity_id, r.connector_id, r.source_type, r.source_id
        FROM entity_sources es
        JOIN raw_records r ON r.id = es.raw_record_id
        ORDER BY es.entity_id, r.source_type, r.source_id
    )
    SELECT n.entity_id, e.entity_type, e.title, e.occurred_at, n.hops, n.via,
           c.connector_id, c.source_type, c.source_id
    FROM nearest n
    JOIN entities e ON e.id = n.entity_id
    LEFT JOIN citation c ON c.entity_id = n.entity_id
    -- Undated entities last rather than first: they are context, and a
    -- timeline that opened with everything it could not date would bury the
    -- chain it exists to show.
    ORDER BY e.occurred_at ASC NULLS LAST, n.hops, e.id
    LIMIT (SELECT cap FROM limits);
$$;

REVOKE ALL ON FUNCTION timeline(uuid, uuid, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION timeline(uuid, uuid, int, int) TO hippo_agent;
GRANT EXECUTE ON FUNCTION timeline(uuid, uuid, int, int) TO hippo_api;
