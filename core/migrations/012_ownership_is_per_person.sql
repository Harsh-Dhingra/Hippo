-- ============================================================
-- Hippo — Your things are yours, whichever account recorded them (P1-SRF-2)
--
-- Found by driving the demo. Alice's login resolves to whichever of her
-- principals sorts first — her Jira account, as it happens — while the agent
-- had recorded her trace and her proposed action against her Slack account.
-- Both screens were empty, and neither was wrong about anything it did: they
-- were asking "does this row belong to principal X" when the question is "does
-- this row belong to the person holding principal X".
--
-- Migration 008 answered exactly that question for the permission filter and
-- this did not carry the reasoning across. It is the same fix: a person is the
-- set of accounts sharing an identity_id, and ownership is per person.
--
-- WHY NOT REUSE _expanded_principals
--
-- That closure walks upward into groups, which is right for permissions — a
-- grant to #deals-acme reaches every member. It is wrong for ownership: an
-- action is not proposed by a group, and a trace is not a group's question.
-- Widening ownership to group members would show one person's questions to
-- their whole team, which is a different feature and not this one.
--
-- So this is a separate, deliberately narrower helper. Both exist because the
-- two questions are genuinely different, and collapsing them would answer one
-- of them wrongly.
-- ============================================================

-- Ungranted and SECURITY INVOKER, like the other internal predicates: it is a
-- building block for the granted functions below, not a surface of its own.
CREATE FUNCTION _same_person(p_principal uuid)
RETURNS TABLE (principal_id uuid)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT p_principal
    UNION
    SELECT sibling.id
    FROM principals me
    JOIN principals sibling ON sibling.identity_id = me.identity_id
    WHERE me.id = p_principal AND me.identity_id IS NOT NULL;
$$;

-- Postgres grants EXECUTE on a new function to PUBLIC by default, which is
-- how an internal predicate becomes a surface nobody meant to publish. 003
-- revokes for the same reason; the role leak test is what catches forgetting.
REVOKE ALL ON FUNCTION _same_person(uuid) FROM PUBLIC;

COMMENT ON FUNCTION _same_person(uuid) IS
    'Every account one human holds. Ownership, not permission: it never walks '
    'into groups. See _expanded_principals for the permission closure.';

-- ------------------------------------------------------------
-- Traces belong to the person who asked
-- ------------------------------------------------------------
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
    WHERE t.principal_id IN (SELECT sp.principal_id FROM _same_person(p_principal) sp)
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
    WHERE t.id = p_trace
      AND t.principal_id IN (SELECT sp.principal_id FROM _same_person(p_principal) sp)
$$;

-- ------------------------------------------------------------
-- Actions belong to the person who asked for them
-- ------------------------------------------------------------
-- The API reads and updates actions directly, so it needs the same predicate
-- available as a granted function rather than as SQL it has to restate at four
-- call sites and keep in step.
CREATE FUNCTION my_principals(p_principal uuid)
RETURNS TABLE (principal_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT sp.principal_id FROM _same_person(p_principal) sp;
$$;

REVOKE ALL ON FUNCTION my_principals(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_principals(uuid) TO hippo_api;
