-- ============================================================
-- Hippo — Closing two gaps the threat model found (P2-SEC-1)
--
-- 1. THE API COULD READ EVERY ENTITY TITLE
--
-- 010 granted hippo_api SELECT on entities for one reason: the approval screen
-- says "jira.comment on Acme renewal blocked on legal review", and that title
-- came from a join.
--
-- But an entity title is content. A Jira issue's title is its summary; a Slack
-- message's is its first line. So the process that serves users held a read
-- path to a projection of the corpus that does not go through
-- visible_chunks(), for the sake of one label. Nothing exploited it — every
-- query scopes to the caller's own actions — but "nothing currently exploits
-- it" is exactly the sentence that precedes a leak, and CLAUDE.md rule 1 is
-- specifically about not having a second read path to review.
--
-- The fix is to stop needing the join. The agent already composes a one-line
-- summary when it proposes (agent/actions.py describe()), from sources the
-- asker could see, and that summary is what a person approves from. Storing it
-- on the row makes the action self-describing, and the grant goes away.
--
-- 2. "connectors.config HOLDS NO TOKENS" WAS ONLY A SENTENCE
--
-- CLAUDE.md says it and sync/worker.py reads credentials only from the
-- environment, so nothing puts one there today. But a future connector, or an
-- operator following a half-remembered example, easily could — and a token in
-- a jsonb column is a token in every backup, every replica and every
-- pg_dump someone pastes into an issue.
--
-- A CHECK is the difference between a convention and a rule. It matches on key
-- names rather than trying to recognise secret-shaped values, because the
-- former is decidable and the latter is not.
-- ============================================================

-- ------------------------------------------------------------
-- 1. A self-describing action
-- ------------------------------------------------------------
ALTER TABLE actions ADD COLUMN summary text;

COMMENT ON COLUMN actions.summary IS
    'One line a person can approve from, composed by the agent from sources '
    'the asker could see. Stored so the approval surface needs no read path '
    'into entities.';

-- Backfill what exists, from the title the join used to supply. Runs as the
-- migration owner, which can still read entities; hippo_api never could after
-- the revoke below.
UPDATE actions a
SET summary = a.action_type || ' on ' || COALESCE(e.title, a.target_entity::text, 'the target')
FROM entities e
WHERE a.summary IS NULL AND e.id = a.target_entity;

UPDATE actions SET summary = action_type WHERE summary IS NULL;

REVOKE SELECT ON entities FROM hippo_api;

-- ------------------------------------------------------------
-- 2. Credentials cannot be stored next to config
-- ------------------------------------------------------------
-- Deliberately about key names, not values. Recognising a secret by its shape
-- is guesswork that fails open; a key called "token" is unambiguous, and an
-- operator who hits this constraint has learned the rule at the moment it
-- mattered rather than after a dump.
--
-- The list is the vocabulary people actually use. It is not exhaustive and
-- cannot be — this is one layer, and sync/worker.py reading credentials only
-- from the environment is the other.
CREATE FUNCTION config_names_a_secret(p_config jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM jsonb_object_keys(COALESCE(p_config, '{}'::jsonb)) AS key
        WHERE lower(key) ~ '(token|secret|password|passwd|api_?key|credential|private_?key|client_?secret|bearer|refresh)'
    );
$$;

REVOKE ALL ON FUNCTION config_names_a_secret(jsonb) FROM PUBLIC;

COMMENT ON FUNCTION config_names_a_secret(jsonb) IS
    'True when a config object has a key that names a credential. Used by the '
    'CHECK on connectors.config; see CLAUDE.md, no secrets in the database.';

ALTER TABLE connectors ADD CONSTRAINT connectors_config_holds_no_secrets
    CHECK (NOT config_names_a_secret(config));
