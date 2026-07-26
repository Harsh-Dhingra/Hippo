-- ============================================================
-- Hippo — Skills that run without anybody asking (P3-AGT-2)
--
-- A standing digest is the first thing in this system that reads content with
-- nobody present. Every other read is a person asking a question, and the
-- permission filter answers "what may *you* see". A scheduled run has to answer
-- the same question, which means it needs a whose.
--
-- WHOSE PERMISSIONS
--
-- `runs_as` is required and references principals. Not nullable, not defaulted,
-- no notion of a system principal that sees everything. A schedule is somebody's
-- standing question, run on their behalf and visible to them, and its answer is
-- exactly what they would have got by asking it themselves.
--
-- The alternative — a service principal with broad access, delivering summaries
-- to a channel — is how a memory system becomes the thing that leaks. It is not
-- expressible here: there is no row shape for it.
--
-- WHY NOT CRON
--
-- Five fields of cron syntax is a parser, an ambiguity about time zones, and a
-- class of schedule nobody in this system needs. The fragment's own examples are
-- "Monday pipeline summary" and "standing digests", so the vocabulary is hourly,
-- daily and weekly with an anchor. A schedule anybody can read at a glance is
-- worth more here than one that can express every fifth minute of February.
--
-- Times are UTC. A schedule that silently shifted with daylight saving would
-- deliver Monday's summary on Sunday twice a year.
-- ============================================================

CREATE TABLE skill_schedules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The skill's name, not a foreign key: skills are files, and a schedule
    -- naming one that has been removed should be reported rather than deleted
    -- by a cascade nobody asked for.
    skill        text NOT NULL,
    -- Whose permissions. Required, and the whole reason this table can exist.
    runs_as      uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    inputs       jsonb NOT NULL DEFAULT '{}',

    cadence      text NOT NULL,
    -- UTC. Meaningful for daily and weekly; ignored for hourly.
    at_hour      int NOT NULL DEFAULT 9,
    -- ISO weekday, 1 = Monday. Meaningful for weekly only.
    at_weekday   int NOT NULL DEFAULT 1,

    enabled      boolean NOT NULL DEFAULT true,
    last_run_at  timestamptz,
    last_error   text,
    next_run_at  timestamptz NOT NULL DEFAULT now(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   uuid REFERENCES principals(id),

    CONSTRAINT skill_schedules_cadence_known
        CHECK (cadence IN ('hourly', 'daily', 'weekly')),
    CONSTRAINT skill_schedules_hour_is_a_hour CHECK (at_hour BETWEEN 0 AND 23),
    CONSTRAINT skill_schedules_weekday_is_a_weekday CHECK (at_weekday BETWEEN 1 AND 7),
    -- One standing question per person per skill. A second is either a mistake
    -- or a sign the skill needs an input, and both are better said out loud.
    CONSTRAINT skill_schedules_one_per_person UNIQUE (skill, runs_as)
);

CREATE INDEX skill_schedules_due_idx ON skill_schedules (next_run_at) WHERE enabled;
CREATE INDEX skill_schedules_principal_idx ON skill_schedules (runs_as);

COMMENT ON COLUMN skill_schedules.runs_as IS
    'Whose permissions the run uses. Required: there is no system principal '
    'that sees everything, and no row shape that would express one.';

-- ------------------------------------------------------------
-- When next
-- ------------------------------------------------------------
-- In SQL rather than Python so the claim is one statement. A claim that read
-- rows, computed in the application and wrote back would have a window between
-- the read and the write in which a second worker could take the same row.
CREATE FUNCTION next_skill_run(
    p_cadence text,
    p_hour    int,
    p_weekday int,
    p_from    timestamptz
) RETURNS timestamptz
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $$
DECLARE
    base timestamptz;
    ahead timestamptz;
BEGIN
    IF p_cadence = 'hourly' THEN
        RETURN date_trunc('hour', p_from AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
               + interval '1 hour';
    END IF;

    base := (date_trunc('day', p_from AT TIME ZONE 'UTC') + make_interval(hours => p_hour))
            AT TIME ZONE 'UTC';

    IF p_cadence = 'daily' THEN
        RETURN CASE WHEN base > p_from THEN base ELSE base + interval '1 day' END;
    END IF;

    -- Weekly: the next p_weekday at p_hour, and never the same instant twice.
    ahead := base + make_interval(
        days => ((p_weekday - extract(isodow FROM p_from AT TIME ZONE 'UTC')::int) + 7) % 7
    );
    RETURN CASE WHEN ahead > p_from THEN ahead ELSE ahead + interval '7 days' END;
END;
$$;

-- ------------------------------------------------------------
-- Claiming what is due
-- ------------------------------------------------------------
-- The UPDATE is the claim. next_run_at moves forward in the same statement
-- that reads the row, so two workers polling together cannot both take one —
-- the same reason the jobs table uses SKIP LOCKED, applied to a smaller table
-- that does not need one.
CREATE FUNCTION claim_due_skills(p_limit int DEFAULT 20)
RETURNS TABLE (
    id       uuid,
    skill    text,
    runs_as  uuid,
    inputs   jsonb
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH due AS (
        SELECT s.id
        FROM skill_schedules s
        WHERE s.enabled AND s.next_run_at <= now()
        ORDER BY s.next_run_at
        LIMIT LEAST(GREATEST(COALESCE(p_limit, 20), 1), 500)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE skill_schedules s
    SET next_run_at = next_skill_run(s.cadence, s.at_hour, s.at_weekday, now())
    FROM due
    WHERE s.id = due.id
    RETURNING s.id, s.skill, s.runs_as, s.inputs;
$$;

REVOKE ALL ON FUNCTION next_skill_run(text, int, int, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION claim_due_skills(int) FROM PUBLIC;

-- The API process, not the sync worker. ARCHITECTURE section 2 says the only
-- component that talks to the model is the agent service, and running a skill
-- is a model call — so putting this in the worker would mean the process
-- holding Slack and Jira credentials also held a model key. The worker gains
-- nothing here at all.
--
-- Neither role gains a way to read content either: a run goes through the
-- agent, as runs_as, through visible_chunks().
GRANT EXECUTE ON FUNCTION claim_due_skills(int) TO hippo_api;
GRANT EXECUTE ON FUNCTION next_skill_run(text, int, int, timestamptz) TO hippo_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON skill_schedules TO hippo_api;

-- ------------------------------------------------------------
-- Reading your own schedules
-- ------------------------------------------------------------
-- Scoped to the reader like everything else. A list of other people's standing
-- questions is a list of what they care about, which is not nothing.
CREATE FUNCTION my_schedules(p_principal uuid)
RETURNS TABLE (
    id          uuid,
    skill       text,
    runs_as     uuid,
    inputs      jsonb,
    cadence     text,
    at_hour     int,
    at_weekday  int,
    enabled     boolean,
    last_run_at timestamptz,
    last_error  text,
    next_run_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT s.id, s.skill, s.runs_as, s.inputs, s.cadence, s.at_hour, s.at_weekday,
           s.enabled, s.last_run_at, s.last_error, s.next_run_at
    FROM skill_schedules s
    WHERE s.runs_as IN (SELECT sp.principal_id FROM _same_person(p_principal) sp)
    ORDER BY s.skill;
$$;

REVOKE ALL ON FUNCTION my_schedules(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION my_schedules(uuid) TO hippo_api;
