-- ============================================================
-- Hippo — The query trace (P1-AGT-4)
--
-- ARCHITECTURE §9 calls the trace a feature, not debug output, and §12 point 4
-- makes "every step of all three demos visible in the trace" part of done. It
-- is also the security story: §9 says the only egress is prompts to the model
-- API, and that claim is worth nothing unless someone can check what was in
-- them.
--
-- WHAT IS STORED, AND WHAT IS DELIBERATELY NOT
--
-- §9 asks for two things that pull against each other: "records exactly what
-- went into every prompt" and "entity IDs only, not content, to keep traces
-- cheap". Copying every retrieved chunk into a trace row would double the
-- corpus on disk and, worse, make a second copy of content that the permission
-- filter no longer guards.
--
-- So the trace stores what is needed to *reconstruct* a prompt rather than a
-- copy of one: the operator-authored system prompt, which contains no synced
-- content by construction (CLAUDE.md rule 6 keeps content out of that
-- channel), and the ordered list of chunks that were fenced into the user
-- turn, each with the content hash it had at the time. Rendering those chunks
-- through the same function that built the prompt reproduces it exactly, and
-- a hash that no longer matches says the chunk changed since — which a stored
-- copy would have hidden.
--
-- READING A TRACE
--
-- Traces contain a person's questions and answers, so reading them is a
-- permission question, and this project has one answer to those: a granted
-- function over ungranted tables. hippo_agent gets INSERT on the tables and
-- EXECUTE on two SECURITY DEFINER readers that filter on ownership. No SELECT
-- grant, so there is no query a later helper function could write that returns
-- someone else's trace.
--
-- v0 scope: you read your own traces. Cross-user visibility for an operator is
-- a real need and a separate decision about who counts as an operator; it is
-- not something to add by leaving the door open now.
-- ============================================================

CREATE TABLE query_traces (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id      uuid NOT NULL REFERENCES principals(id),
    question          text NOT NULL,
    -- The RetrievalPlan, including its rationale: why this query searched the
    -- way it did, which is the half of a trace that explains the other half.
    plan              jsonb NOT NULL,
    route             text NOT NULL,
    -- The ordered steps, each with its own duration. Small, always read whole,
    -- never filtered on — a jsonb array rather than a table it would have to
    -- be joined back from.
    steps             jsonb NOT NULL DEFAULT '[]'::jsonb,
    system_prompt     text,
    model             text,
    provider          text,
    input_tokens      int NOT NULL DEFAULT 0,
    output_tokens     int NOT NULL DEFAULT 0,
    answer            text,
    citations         uuid[] NOT NULL DEFAULT '{}',
    refused           boolean NOT NULL DEFAULT false,
    action_id         uuid REFERENCES actions(id),
    error             text,
    duration_ms       int NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT query_traces_route_known
        CHECK (route IN ('synthesize', 'propose', 'nothing_visible', 'error'))
);
CREATE INDEX ON query_traces (principal_id, created_at DESC);
CREATE INDEX ON query_traces (action_id) WHERE action_id IS NOT NULL;

-- What retrieval returned, in rank order. Entity ids and hashes, no content.
CREATE TABLE trace_retrievals (
    trace_id          uuid NOT NULL REFERENCES query_traces(id) ON DELETE CASCADE,
    rank              int NOT NULL,
    chunk_id          uuid NOT NULL,
    entity_id         uuid NOT NULL,
    entity_type       text,
    entity_title      text,
    content_hash      text,
    score             double precision NOT NULL,
    retrieval_modes   text[] NOT NULL DEFAULT '{}',
    cited             boolean NOT NULL DEFAULT false,

    PRIMARY KEY (trace_id, rank)
);

-- ------------------------------------------------------------
-- Grants: write freely, read only your own
-- ------------------------------------------------------------
GRANT INSERT ON query_traces, trace_retrievals TO hippo_agent;

CREATE FUNCTION my_traces(p_principal uuid, p_limit int DEFAULT 50)
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

CREATE FUNCTION my_trace(p_principal uuid, p_trace uuid)
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

REVOKE ALL ON FUNCTION my_traces(uuid, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION my_trace(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_traces(uuid, int) TO hippo_agent;
GRANT EXECUTE ON FUNCTION my_trace(uuid, uuid) TO hippo_agent;
