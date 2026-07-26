-- ============================================================
-- Hippo — Model-assisted matching, kept where it cannot do harm (P3-RES-1)
--
-- ARCHITECTURE section 6 deferred fuzzy matching out of v0 and said what shape
-- it would have to take when it arrived: "its edges carry provenance='model'
-- and confidence<1.0 so they can be distrusted or filtered wholesale". Two
-- words in that sentence decide this whole migration.
--
-- EDGES, NOT MERGES
--
-- "Filtered wholesale" is only possible if a model's conclusion is additive. A
-- merge rewrites the graph: two entities become one, their sources are
-- repointed, and undoing it means reconstructing from raw_records and hoping
-- nothing referenced the id that went away. An edge is a row you can DELETE.
--
-- So a model never merges anything. It proposes a `same_as` edge, and the
-- graph walk in visible_chunks() follows edges — which is the second reason
-- this is safe, below.
--
-- WHY THIS CANNOT LEAK
--
-- The recursive walk joins visible_entities at every hop. An edge can only
-- reach an entity the asker already holds a grant for, so a wrong inference
-- reorders results and can never surface something new. A model here costs
-- ranking quality when it is wrong, not confidentiality.
--
-- WHAT A MODEL IS NEVER ALLOWED NEAR
--
-- principals.identity_id. That column feeds _expanded_principals(), which *is*
-- the permission filter's notion of who you are — a model setting it would be
-- a language model granting access to somebody's account. It is not behind the
-- visible_entities guard, because it is upstream of it.
--
-- Person identity stays exactly where it was: an exact match on a verified
-- email, in resolver/resolution.py, with no model involved. This table cannot
-- express a principal at all; there is no column for one.
--
-- WHY A SUGGESTION LOG AND NOT ONLY EDGES
--
-- A rejected pair is worth keeping. Without it, every run re-asks the model
-- about the pair a person already said no to, which costs money and quietly
-- overrides them. The row also records *why* — the score, the method, the
-- model's own words — because "the model said so" is not a reason anybody can
-- act on six months later.
-- ============================================================

CREATE TABLE entity_matches (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Ordered by id so a pair is one row whichever way round it was proposed.
    left_id      uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    right_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,

    -- How it was decided: 'exact' and 'heuristic' need no model, 'model' does.
    method       text NOT NULL,
    -- What the method believed. Never 1.0 for a model: rule 5.
    confidence   real NOT NULL,
    -- The model's own sentence, or the rule that fired. Stored so a person
    -- reviewing this in six months has something to act on.
    reason       text,

    -- NULL means nobody has looked. A human decision outranks any rerun.
    decided_by   uuid REFERENCES principals(id),
    decided_at   timestamptz,
    accepted     boolean,

    applied_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT entity_matches_method_known
        CHECK (method IN ('exact', 'heuristic', 'model')),
    CONSTRAINT entity_matches_confidence_in_range
        CHECK (confidence > 0.0 AND confidence <= 1.0),
    -- CLAUDE.md rule 5, at the source of the inference rather than only on the
    -- edge it produces.
    CONSTRAINT entity_matches_model_is_never_certain
        CHECK (method <> 'model' OR confidence < 1.0),
    -- An entity is trivially itself, and a self-match would produce a self-edge
    -- that makes the graph walk loop.
    CONSTRAINT entity_matches_is_between_two_things CHECK (left_id <> right_id),
    -- Canonical ordering, so (a,b) and (b,a) cannot both exist.
    CONSTRAINT entity_matches_is_ordered CHECK (left_id < right_id),
    CONSTRAINT entity_matches_one_per_pair UNIQUE (left_id, right_id)
);

CREATE INDEX entity_matches_undecided_idx ON entity_matches (created_at)
    WHERE decided_at IS NULL;
CREATE INDEX entity_matches_right_idx ON entity_matches (right_id);

COMMENT ON TABLE entity_matches IS
    'Proposed identity matches between entities. Never between principals: '
    'principals.identity_id is a permission input and stays deterministic.';

COMMENT ON COLUMN entity_matches.accepted IS
    'A human decision. Outranks any rerun, so a person saying no is not undone '
    'by the next resolver pass.';

-- The resolver proposes and applies; the API lets a person decide.
GRANT SELECT, INSERT, UPDATE ON entity_matches TO hippo_resolver;
GRANT SELECT, UPDATE ON entity_matches TO hippo_api;

-- ------------------------------------------------------------
-- Reading a person's own view of what was inferred
-- ------------------------------------------------------------
-- Scoped, like everything else. The titles come from entities, which the API
-- role cannot read (migration 013 revoked it, because a title is content), so
-- this function is how a review screen gets them — for entities the reader can
-- already see and no others.
CREATE FUNCTION my_entity_matches(p_principal uuid, p_limit int DEFAULT 100)
RETURNS TABLE (
    id           uuid,
    left_id      uuid,
    left_title   text,
    right_id     uuid,
    right_title  text,
    entity_type  text,
    method       text,
    confidence   real,
    reason       text,
    accepted     boolean,
    applied_at   timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT m.id, m.left_id, l.title, m.right_id, r.title, l.entity_type,
           m.method, m.confidence, m.reason, m.accepted, m.applied_at
    FROM entity_matches m
    JOIN entities l ON l.id = m.left_id
    JOIN entities r ON r.id = m.right_id
    WHERE l.id IN (SELECT ve.entity_id FROM _visible_entity_ids(p_principal) ve)
      AND r.id IN (SELECT ve.entity_id FROM _visible_entity_ids(p_principal) ve)
    ORDER BY m.confidence DESC, m.created_at DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 1000);
$$;

REVOKE ALL ON FUNCTION my_entity_matches(uuid, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_entity_matches(uuid, int) TO hippo_api;

-- ------------------------------------------------------------
-- Forgetting every inference, in one statement
-- ------------------------------------------------------------
-- "Distrusted or filtered wholesale" has to be one command or nobody will use
-- it under pressure. An operator who stops trusting the model — a bad release,
-- a change of provider, a run against the wrong corpus — gets the graph back
-- to deterministic facts and loses nothing else, because a model never wrote
-- anything but these edges.
CREATE FUNCTION forget_model_inferences() RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM edges WHERE provenance = 'model';
    GET DIAGNOSTICS removed = ROW_COUNT;
    -- The suggestions stay. They are the record of what was believed and why,
    -- and a human's accept or reject on them is a decision worth keeping even
    -- when the edge it produced has been withdrawn.
    UPDATE entity_matches SET applied_at = NULL WHERE method = 'model';
    RETURN removed;
END;
$$;

REVOKE ALL ON FUNCTION forget_model_inferences() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION forget_model_inferences() TO hippo_resolver;
