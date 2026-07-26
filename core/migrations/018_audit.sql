-- ============================================================
-- Hippo — The audit surface (P2-GOV-2)
--
-- ARCHITECTURE §4 point 4 says "every state transition is a timestamped row;
-- the audit log is the table itself". That was true and it was not enough.
--
-- WHAT THE ACTIONS TABLE CANNOT ANSWER
--
-- It holds the *current* state of each action and the identity of whoever last
-- decided. So it answers "what is this action now" and cannot answer:
--
--     who declined this before someone else approved it?
--     how long did it sit pending?
--     was this executed twice, or retried once?
--     what did the payload say when it was approved?
--
-- Those are the questions an audit is for, and none of them survive an UPDATE.
-- A table whose rows are overwritten is a state store; an audit log is an
-- append-only record of transitions, and they are different things wearing
-- similar names.
--
-- APPEND-ONLY, ENFORCED
--
-- action_events takes no UPDATE or DELETE grant from any service role. Not
-- because nobody would, but because the value of an audit log is exactly
-- proportional to how hard it is to edit — a log the application can rewrite
-- answers "what do we currently claim happened".
--
-- Retention is therefore an operator action rather than an application one:
-- deletion needs the owner role, and core/audit.py's purge is something a
-- scheduled task runs deliberately, not something a request can trigger.
--
-- WRITTEN BY A TRIGGER
--
-- Every writer of actions would otherwise have to remember to log, and the one
-- that forgets is the one whose transition matters. A trigger cannot be
-- forgotten by a future caller, and it captures the payload as it stood at the
-- moment of the change rather than as it stands now.
-- ============================================================

CREATE TABLE action_events (
    id            bigserial PRIMARY KEY,
    action_id     uuid NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    at            timestamptz NOT NULL DEFAULT now(),
    from_status   text,
    to_status     text NOT NULL,
    -- Who, in whichever of the three senses applies. All three are nullable
    -- because a transition can be a person, a policy, or the executor, and
    -- pretending otherwise is what makes an audit log lie.
    actor         uuid REFERENCES principals(id),
    actor_policy  text,
    -- The action as it stood at this moment, so a later edit cannot rewrite
    -- what was approved.
    snapshot      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX action_events_action_idx ON action_events (action_id, at);
CREATE INDEX action_events_at_idx ON action_events (at DESC);
CREATE INDEX action_events_status_idx ON action_events (to_status, at DESC);

COMMENT ON TABLE action_events IS
    'Append-only. No service role holds UPDATE or DELETE: a log the application '
    'can rewrite answers "what do we currently claim happened".';

-- SECURITY DEFINER, so the trigger writes as the table owner. That is what
-- lets action_events take no INSERT grant from any service role while still
-- being written on every transition: the roles cause events, and none of them
-- can author one directly. Without it, append-only would have meant unwritable.
CREATE FUNCTION record_action_event() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- Only transitions. An UPDATE that touches a payload without moving the
    -- status is not an event in the life of the action, and logging it would
    -- bury the ones that are.
    IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;

    INSERT INTO action_events (action_id, from_status, to_status, actor, actor_policy, snapshot)
    VALUES (
        NEW.id,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
        NEW.status,
        -- The person responsible for *this* transition, which is not always
        -- the same column: approving names an approver, declining a decliner,
        -- rolling back whoever asked for it.
        CASE NEW.status
            WHEN 'approved' THEN NEW.approved_by
            WHEN 'declined' THEN NEW.declined_by
            WHEN 'rolled_back' THEN NEW.rolled_back_by
            WHEN 'pending' THEN NEW.requested_by
            ELSE NULL
        END,
        CASE WHEN NEW.status = 'approved' THEN NEW.approved_by_policy ELSE NULL END,
        jsonb_build_object(
            'action_type', NEW.action_type,
            'risk_class', NEW.risk_class,
            'summary', NEW.summary,
            'payload', NEW.payload,
            'target_entity', NEW.target_entity,
            'error', NEW.error
        )
    );
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION record_action_event() FROM PUBLIC;

CREATE TRIGGER actions_record_event
    AFTER INSERT OR UPDATE ON actions
    FOR EACH ROW EXECUTE FUNCTION record_action_event();

-- Backfill one event per action that already exists, so the log does not start
-- with a gap. from_status is NULL: we know where these are, not how they got
-- there, and inventing a history would be worse than admitting it began here.
INSERT INTO action_events (action_id, at, from_status, to_status, actor, actor_policy, snapshot)
SELECT a.id, a.created_at, NULL, a.status,
       coalesce(a.approved_by, a.declined_by, a.requested_by), a.approved_by_policy,
       jsonb_build_object(
           'action_type', a.action_type, 'risk_class', a.risk_class,
           'summary', a.summary, 'payload', a.payload,
           'target_entity', a.target_entity, 'error', a.error,
           'backfilled', true
       )
FROM actions a;

-- ------------------------------------------------------------
-- Reading the log
-- ------------------------------------------------------------
-- Scoped to the reader, like everything else. An audit log of other people's
-- actions is a list of things they can see.
CREATE FUNCTION my_action_events(
    p_principal uuid,
    p_status    text DEFAULT NULL,
    p_since     timestamptz DEFAULT NULL,
    p_limit     int DEFAULT 200
)
RETURNS TABLE (
    id           bigint,
    action_id    uuid,
    at           timestamptz,
    from_status  text,
    to_status    text,
    actor        uuid,
    actor_policy text,
    action_type  text,
    risk_class   text,
    summary      text,
    snapshot     jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT e.id, e.action_id, e.at, e.from_status, e.to_status, e.actor, e.actor_policy,
           e.snapshot ->> 'action_type', e.snapshot ->> 'risk_class', e.snapshot ->> 'summary',
           e.snapshot
    FROM action_events e
    JOIN actions a ON a.id = e.action_id
    WHERE a.requested_by IN (SELECT sp.principal_id FROM _same_person(p_principal) sp)
      AND (p_status IS NULL OR e.to_status = p_status)
      AND (p_since IS NULL OR e.at >= p_since)
    ORDER BY e.at DESC, e.id DESC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 200), 1), 5000);
$$;

REVOKE ALL ON FUNCTION my_action_events(uuid, text, timestamptz, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_action_events(uuid, text, timestamptz, int) TO hippo_api;

-- Deliberately no INSERT, UPDATE or DELETE grant on action_events to anyone.
-- The trigger writes it as the table owner; nothing else writes it at all, and
-- retention deletion is an operator action rather than an application one.
