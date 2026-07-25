-- Reverts 012_ownership_is_per_person.sql by restoring the 009 bodies.
--
-- Reverting narrows what a person can see of their own history: someone whose
-- login resolves to a different account than the one that recorded a trace
-- stops finding it. That direction loses nothing and reveals nothing, which
-- is why this is reversible at all.

DROP FUNCTION IF EXISTS my_principals(uuid);

CREATE OR REPLACE FUNCTION my_traces(p_principal uuid, p_limit int DEFAULT 50)
RETURNS TABLE (
    id           uuid,
    question     text,
    route        text,
    model        text,
    answer       text,
    refused      boolean,
    action_id    uuid,
    error        text,
    input_tokens int,
    output_tokens int,
    duration_ms  int,
    created_at   timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.question, t.route, t.model, t.answer, t.refused, t.action_id,
           t.error, t.input_tokens, t.output_tokens, t.duration_ms, t.created_at
    FROM query_traces t
    WHERE t.principal_id = p_principal
    ORDER BY t.created_at DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 500);
$$;

CREATE OR REPLACE FUNCTION my_trace(p_principal uuid, p_trace uuid)
RETURNS TABLE (
    id            uuid,
    question      text,
    plan          jsonb,
    route         text,
    steps         jsonb,
    system_prompt text,
    model         text,
    provider      text,
    input_tokens  int,
    output_tokens int,
    answer        text,
    citations     uuid[],
    refused       boolean,
    action_id     uuid,
    error         text,
    duration_ms   int,
    created_at    timestamptz,
    retrievals    jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.question, t.plan, t.route, t.steps, t.system_prompt, t.model,
           t.provider, t.input_tokens, t.output_tokens, t.answer, t.citations,
           t.refused, t.action_id, t.error, t.duration_ms, t.created_at,
           COALESCE(
               (SELECT jsonb_agg(
                           jsonb_build_object(
                               'rank', r.rank,
                               'chunk_id', r.chunk_id,
                               'entity_id', r.entity_id,
                               'entity_type', r.entity_type,
                               'entity_title', r.entity_title,
                               'content_hash', r.content_hash,
                               'score', r.score,
                               'retrieval_modes', r.retrieval_modes,
                               'cited', r.cited
                           ) ORDER BY r.rank)
                FROM trace_retrievals r WHERE r.trace_id = t.id),
               '[]'::jsonb
           )
    FROM query_traces t
    WHERE t.id = p_trace AND t.principal_id = p_principal;
$$;

DROP FUNCTION IF EXISTS _same_person(uuid);
